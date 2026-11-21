import http from 'node:http';
import {randomUUID, timingSafeEqual} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {createReadStream} from 'node:fs';
import {stat} from 'node:fs/promises';
import {join} from 'node:path';
import {pipeline} from 'node:stream/promises';
import {readConfig, WRITE_TOOLS, enabledTools, ROOM_PUBLICATION_TOOL, ROOM_PUBLICATION_TIMEOUT} from './common.mjs';

const same = (a, b) => typeof a === 'string' && typeof b === 'string' && a.length >= 32 && Buffer.byteLength(a) === Buffer.byteLength(b) && timingSafeEqual(Buffer.from(a), Buffer.from(b));
const reply = (res, status, data) => {res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}); res.end(JSON.stringify(data));};
async function json(req) {
  const chunks = []; let size = 0;
  for await (const chunk of req) {size += chunk.length; if (size > 256000) throw Object.assign(new Error('Request too large.'), {status: 413}); chunks.push(chunk);}
  try {
    const value = JSON.parse(Buffer.concat(chunks).toString());
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
    return value;
  } catch {throw Object.assign(new Error('Expected a JSON object.'), {status: 400});}
}
export function createHub(config, archiveDirectory = '/publication-archives') {
  if (!config.serverToken || config.serverToken.length < 32 || !config.clients?.length) throw new Error('Private server and client credentials are required.');
  if (config.clients.some(client => !client.id || !client.token || client.token.length < 32) || new Set([config.serverToken, ...config.clients.map(client => client.token)]).size !== config.clients.length + 1 || new Set(config.clients.map(client => client.id)).size !== config.clients.length) throw new Error('Client IDs and strong credentials must be distinct.');
  const sessions = new Map(), jobs = new Map();
  const online = session => Date.now() - session.lastSeen < 45000;
  const fail = (status, message) => {throw Object.assign(new Error(message), {status});};
  const publicSession = session => ({id: session.id, client: session.client.id, label: session.client.label, lastSeen: session.lastSeen,
    model: session.model, writeEnabled: session.writeEnabled, publicationReader: session.publicationReader, tools: session.tools});
  const server = http.createServer(async (req, res) => {
    try {
      const route = new URL(req.url, 'http://bridge').pathname;
      if (route === '/health' && req.method === 'GET') return reply(res, 200, {ok: true});
      const token = req.headers.authorization?.replace(/^Bearer /, '');
      const operator = same(token, config.serverToken);
      const client = config.clients.find(item => same(token, item.token));
      if (!operator && !client) return reply(res, 401, {error: 'Unauthorized.'});
      const archive = /^\/archives\/([a-f0-9]{64})$/.exec(route);
      if (archive && req.method === 'GET') {
        const digest = archive[1];
        const allowed = client && [...jobs.values()].some(job => job.name === ROOM_PUBLICATION_TOOL &&
          job.arguments.digest === digest && job.expiresAt > Date.now() && sessions.get(job.sessionId)?.client.id === client.id && job.dispatched);
        if (!allowed) fail(403, 'This archive is not assigned to this workstation.');
        const path = join(archiveDirectory, digest + '.rvt');
        const file = await stat(path).catch(() => null);
        if (!file?.isFile()) fail(404, 'Publication archive not found.');
        res.writeHead(200, {'Content-Type': 'application/octet-stream', 'Content-Length': file.size, 'Cache-Control': 'no-store'});
        try { await pipeline(createReadStream(path), res); } catch {res.destroy();}
        return;
      }
      if (route === '/sessions' && req.method === 'GET' && operator) return reply(res, 200, {sessions: [...sessions.values()].filter(online).map(publicSession)});
      if (route === '/register' && req.method === 'POST' && client) {
        const data = await json(req);
        if (!Array.isArray(data.tools) || data.tools.length > 100 || data.tools.some(tool => !tool || typeof tool.name !== 'string' || typeof tool.inputSchema !== 'object')) fail(400, 'Invalid MCP tool catalog.');
        for (const session of sessions.values()) if (session.client.id === client.id) {
          for (const job of jobs.values()) if (job.sessionId === session.id) job.finish({isError: true, content: [{type: 'text', text: 'Client reconnected. Request was not replayed.'}]});
          sessions.delete(session.id);
        }
        const id = randomUUID();
        const writeEnabled = client.allowWrites === true && data.writeEnabled === true;
        sessions.set(id, {id, client, lastSeen: Date.now(), model: data.model, writeEnabled, queue: [],
          publicationReader: client.allowPublications === true && data.tools.some(tool => tool.name === ROOM_PUBLICATION_TOOL),
          tools: data.tools.filter(tool => enabledTools(writeEnabled).includes(tool.name)).map(({name, description, inputSchema}) => {
            const schema = structuredClone(inputSchema);
            for (const key of Object.keys(schema.properties || {})) if (key.startsWith('_sonne')) delete schema.properties[key];
            if (schema.required) schema.required = schema.required.filter(key => !key.startsWith('_sonne'));
            return {name, description, inputSchema: schema};
          })});
        return reply(res, 200, {sessionId: id});
      }
      const data = req.method === 'POST' ? await json(req) : {};
      const session = sessions.get(data.sessionId);
      if (route === '/call' && req.method === 'POST' && operator) {
        if (!session || !online(session)) fail(409, 'The selected Revit session is offline. List sessions again.');
        if (data.arguments !== undefined && (!data.arguments || typeof data.arguments !== 'object' || Array.isArray(data.arguments))) fail(400, 'Tool arguments must be an object.');
        const publication = data.name === ROOM_PUBLICATION_TOOL && session.publicationReader;
        if (!publication && !session.tools.some(tool => tool.name === data.name)) fail(403, 'This tool is not enabled for this session.');
        if (publication && (!/^[a-f0-9]{64}$/.test(data.arguments?.digest) || typeof data.arguments.schedule_id !== 'string')) fail(400, 'Invalid publication archive request.');
        if (WRITE_TOOLS.includes(data.name) && (data.kind !== 'write' || !session.writeEnabled)) fail(403, 'Edits require an explicit write request and an enabled workstation.');
        if (data.kind === 'write' && !WRITE_TOOLS.includes(data.name)) fail(403, 'Use the read route for read-only tools.');
        if (session.queue.length >= 8 || jobs.size >= 64) fail(429, 'Revit request queue is full.');
        if (!data.modelId || session.model?.documentSessionId !== data.modelId) fail(409, 'Select an open document from the current session.');
        const id = randomUUID();
        const timeout = publication ? ROOM_PUBLICATION_TIMEOUT : 30000;
        const timer = setTimeout(() => finish({isError: true, content: [{type: 'text', text: 'Request timed out. No automatic retry was performed. For an edit, the outcome may be unknown: inspect the element before another attempt.'}]}), timeout);
        const finish = result => {clearTimeout(timer); jobs.delete(id); session.queue = session.queue.filter(item => item !== id); if (!res.writableEnded) reply(res, 200, result);};
        jobs.set(id, {id, sessionId: session.id, name: data.name, kind: data.kind || 'read', arguments: data.arguments || {}, modelId: data.modelId, expiresAt: Date.now() + timeout - 2000, finish});
        session.queue.push(id);
        return;
      }
      if (!client || !session || session.client.id !== client.id) fail(403, 'Invalid client session.');
      session.lastSeen = Date.now();
      if (route === '/poll' && req.method === 'POST') {
        session.model = data.model;
        const id = session.queue.shift(), job = jobs.get(id);
        if (job) job.dispatched = true;
        return reply(res, 200, {job: job ? {id, name: job.name, kind: job.kind, arguments: job.arguments, modelId: job.modelId, expiresAt: job.expiresAt} : null});
      }
      if (route === '/result' && req.method === 'POST') {
        const job = jobs.get(data.id);
        if (job && job.sessionId !== session.id) fail(403, 'Invalid job owner.');
        if (!data.result || !Array.isArray(data.result.content)) fail(400, 'Invalid MCP result.');
        if (job) job.finish(data.result);
        return reply(res, 200, {ok: true});
      }
      fail(404, 'Not found.');
    } catch (error) {if (!res.writableEnded) reply(res, error.status || 500, {error: error.status ? error.message : 'Bridge operation failed.'});}
  });
  server.on('close', () => {for (const job of jobs.values()) job.finish({isError: true, content: [{type: 'text', text: 'Bridge stopped.'}]});});
  return server;
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = createHub(await readConfig(process.env.REVIT_REGISTRY || '/data/registry.json'));
  server.listen(Number(process.env.PORT || 9091), process.env.HOST || '127.0.0.1', () => console.log('Sonne Revit router ready.'));
}
