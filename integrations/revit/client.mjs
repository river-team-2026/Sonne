import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {setTimeout as pause} from 'node:timers/promises';
import {createWriteStream} from 'node:fs';
import {link, mkdir, unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {Transform, Readable} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {api, readConfig, READ_TOOLS, WRITE_TOOLS, enabledTools, ROOM_PUBLICATION_TOOL} from './common.mjs';

const config = await readConfig(process.argv[2]);
if (new URL(config.url).protocol !== 'https:') throw new Error('The Windows client requires an HTTPS server URL.');
async function stagePublication(digest, deadline) {
  if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error('Invalid publication digest.');
  const url = new URL('archives/' + digest, config.url.endsWith('/') ? config.url : config.url + '/');
  const directory = join(process.env.LOCALAPPDATA, 'Sonne', 'RevitPublications', 'inbox');
  await mkdir(directory, {recursive: true});
  const path = join(directory, digest + '.rvt'), temporary = path + '.' + randomUUID() + '.tmp';
  const hash = createHash('sha256');
  let size = 0;
  try {
    const response = await fetch(url, {headers: {Authorization: 'Bearer ' + config.token}, redirect: 'error', signal: AbortSignal.timeout(Math.max(1, Math.min(70000, deadline - Date.now())))});
    if (!response.ok) throw new Error('Publication archive download failed (HTTP ' + response.status + ').');
    await pipeline(Readable.fromWeb(response.body), new Transform({transform(chunk, enc, done) {
      size += chunk.length;
      if (size > 512 * 1024 * 1024) return done(new Error('The pilot accepts RVT archives up to 512 MiB.'));
      hash.update(chunk); done(null, chunk);
    }}), createWriteStream(temporary, {flags: 'wx'}));
    if (hash.digest('hex') !== digest) throw new Error('Publication archive checksum mismatch.');
    await link(temporary, path).catch(error => {if (error.code !== 'EEXIST') throw error;});
    return path;
  } finally { await unlink(temporary).catch(() => {}); }
}
const mcp = new Client({name: 'sonne-windows-client', version: '0.1.0'});
const transport = new StdioClientTransport({command: config.command, args: config.args, cwd: config.cwd, stderr: 'inherit'});
await mcp.connect(transport);
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => {await mcp.close(); process.exit(0);});
const {tools} = await mcp.listTools();
async function model() {
  try {
    const response = await mcp.callTool({name: 'get_current_view_info', arguments: {}}, undefined, {timeout: 15000});
    const data = JSON.parse(response.content.find(item => item.type === 'text').text);
    return {connected: true, documentSessionId: data.DocumentSessionId || null, documentTitle: data.DocumentTitle || null, revitVersion: data.RevitVersion || null, guardVersion: data.SonneGuardVersion || null};
  } catch {return {connected: false, documentSessionId: null, documentTitle: null};}
}
let sessionId;
console.log('Windows MCP client started; waiting for Revit and server.');
for (;;) {
  try {
    const current = await model();
    if (!sessionId) {
      const enrollment = await readConfig(process.argv[2]);
      if (enrollment.token === config.token && new URL(enrollment.url).protocol === 'https:') {
        config.url = enrollment.url;
      }
      ({sessionId} = await api(config, 'register', {tools: tools.filter(tool => enabledTools(config.enableWrites === true).includes(tool.name) || (config.enablePublications === true && tool.name === ROOM_PUBLICATION_TOOL)), writeEnabled: config.enableWrites === true, model: current}));
      console.log('Registered Windows session:', sessionId);
    }
    const {job} = await api(config, 'poll', {sessionId, model: current});
    if (job) {
      let result;
      const before = await model();
      const publication = job.name === ROOM_PUBLICATION_TOOL && config.enablePublications === true;
      const allowed = job.kind === 'write' ? config.enableWrites === true && WRITE_TOOLS.includes(job.name) : READ_TOOLS.includes(job.name) || publication;
      if (!allowed || before.documentSessionId !== job.modelId || Date.now() >= job.expiresAt) {
        result = {isError: true, content: [{type: 'text', text: 'The active document changed or the tool is not allowed. List sessions again.'}]};
      } else {
        const args = {...job.arguments, _sonneModelId: job.modelId, _sonneDeadline: job.expiresAt, _sonneOperationId: job.id};
        let staged;
        try {
          if (publication) staged = await stagePublication(job.arguments.digest, job.expiresAt);
          if (Date.now() >= job.expiresAt) throw new Error('Publication request expired before Revit could open the archive.');
          result = await mcp.callTool({name: job.name, arguments: job.name.startsWith('sonne_') ? args : job.arguments,
            _meta: {sonne: {_sonneModelId: job.modelId, _sonneDeadline: job.expiresAt, _sonneOperationId: job.id}}}, undefined, {timeout: publication ? Math.max(1, job.expiresAt - Date.now() + 1000) : 25000});
        } catch (error) {
          if (!publication) throw error;
          result = {isError: true, content: [{type: 'text', text: error.message}]};
        } finally {
          if (staged) await unlink(staged).catch(() => {});
        }
        if ((await model()).documentSessionId !== job.modelId) result = {isError: true, content: [{type: 'text', text: 'The active document changed during the request. Inspect the original element before retrying an edit.'}]};
        if (JSON.stringify(result).length > 64000) result = {isError: true, content: [{type: 'text', text: 'Result exceeds 64 KB. Narrow the element filter.'}]};
      }
      await api(config, 'result', {sessionId, id: job.id, result});
    }
    await pause(1000);
  } catch (error) {console.error('Revit connection:', error.message); sessionId = undefined; await pause(3000);}
}
