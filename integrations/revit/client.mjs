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
import {discoverOnline, discoverRouter} from './discovery.mjs';
import {createClientLogger} from './client-log.mjs';

const log = createClientLogger();
if (log.file) log.write('INFO', 'log_started', 'Revit MCP log: ' + log.file);
let config;
try {
  config = await readConfig(process.argv[2]);
} catch {
  log.write('ERROR', 'config_unreadable', 'Could not read the Revit enrollment JSON. Check -ConfigPath.');
  process.exit(1);
}
if (typeof config?.command !== 'string' || !config.command.trim()) {
  log.write('ERROR', 'client_install_required', 'The Windows MCP bridge is missing from this enrollment. Close Revit and run install-revit-client.cmd using the same -ConfigPath.');
  process.exit(1);
}
const enrollmentToken = config.token;
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
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => {
  log.write('INFO', 'client_stopped', 'Windows MCP client stopped.');
  await mcp.close();
  process.exit(0);
});

function connectionDetail(error) {
  const message = error?.message || '';
  if (/^(?:Invalid client session|Enrollment changed\. Restart the Revit client|Waiting for a Sonne announcement|Expired or invalid Sonne tunnel address|Expired or invalid Revit discovery record\. Waiting for the server)\.?$/.test(message) ||
      /^(?:Internet discovery unavailable \(HTTP \d{3}\)\.|Revit bridge HTTP \d{3})$/.test(message)) return message;
  const code = error?.cause?.code || error?.code;
  if (typeof code === 'string' && /^[A-Z][A-Z0-9_]{1,49}$/.test(code)) return code;
  if (typeof code === 'number' && Number.isSafeInteger(code)) return 'MCP error ' + code;
  if (error?.name === 'TimeoutError' || error?.name === 'AbortError') return 'Request timed out';
  return 'Could not complete the request';
}

let tools;
try {
  await mcp.connect(transport);
  ({tools} = await mcp.listTools());
} catch (error) {
  log.write('ERROR', 'bridge_start_failed', 'Could not start the local MCP bridge: ' + connectionDetail(error));
  await mcp.close().catch(() => {});
  process.exit(1);
}
async function model() {
  try {
    const response = await mcp.callTool({name: 'get_current_view_info', arguments: {}}, undefined, {timeout: 15000});
    const data = JSON.parse(response.content.find(item => item.type === 'text').text);
    return {connected: true, documentSessionId: data.DocumentSessionId || null, documentTitle: data.DocumentTitle || null, revitVersion: data.RevitVersion || null, guardVersion: data.SonneGuardVersion || null};
  } catch {return {connected: false, documentSessionId: null, documentTitle: null};}
}
let sessionId, lastConnectionError, lastDocument, lastModelState;
log.write('INFO', 'bridge_started', 'Windows MCP client started; waiting for Revit and server.');
for (;;) {
  try {
    const current = await model();
    const modelState = current.documentSessionId ? 'model_ready' : current.connected ? 'model_missing_id' : 'revit_unavailable';
    if (modelState !== lastModelState || current.documentSessionId !== lastDocument) {
      if (modelState === 'model_ready') log.write('INFO', modelState, 'Revit model is available for MCP requests.');
      else if (modelState === 'model_missing_id') log.write('WARN', modelState, 'Revit responded without a document ID. Open a model and check the Sonne add-in.');
      else log.write('WARN', modelState, 'Waiting for Revit. Open a model and enable Revit MCP Switch.');
      lastModelState = modelState;
      lastDocument = current.documentSessionId;
    }
    if (!sessionId) {
      const enrollment = await readConfig(process.argv[2]);
      if (enrollment.token !== enrollmentToken) throw new Error('Enrollment changed. Restart the Revit client.');
      const directory = enrollment.discoveryDirectory || process.env.SONNE_REVIT_DISCOVERY_DIR;
      if (/^https:\/\/[a-z0-9-]+\.trycloudflare\.com\/revit\/$/.test(enrollment.url)) {
        try {
          Object.assign(config, await discoverOnline(enrollmentToken));
        } catch (error) {
          if (!directory) {
            await pause(12000);
            throw error;
          }
          Object.assign(config, await discoverRouter(directory, enrollmentToken));
        }
      } else if (directory) {
        Object.assign(config, await discoverRouter(directory, enrollmentToken));
      } else if (new URL(enrollment.url).protocol === 'https:') {
        config.url = enrollment.url;
        config.token = enrollmentToken;
      }
      ({sessionId} = await api(config, 'register', {tools: tools.filter(tool => enabledTools(config.enableWrites === true).includes(tool.name) || (config.enablePublications === true && tool.name === ROOM_PUBLICATION_TOOL)), writeEnabled: config.enableWrites === true, model: current}));
      log.write('INFO', 'session_registered', 'Registered Windows session.');
    }
    const {job} = await api(config, 'poll', {sessionId, model: current});
    lastConnectionError = undefined;
    if (job) {
      const knownTool = READ_TOOLS.includes(job.name) || WRITE_TOOLS.includes(job.name) || job.name === ROOM_PUBLICATION_TOOL;
      const request = {
        tool: knownTool ? job.name : 'unknown',
        kind: job.kind === 'write' ? 'write' : 'read'
      };
      if (typeof job.id === 'string' && /^[a-f0-9-]{36}$/i.test(job.id)) request.requestId = job.id;
      const started = performance.now();
      log.write('INFO', 'tool_received', `Sonne requested ${request.tool} (${request.kind}).`, request);
      let stage = 'execute';
      try {
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
        stage = 'report';
        await api(config, 'result', {sessionId, id: job.id, result});
        const durationMs = Math.round(performance.now() - started);
        const outcome = result.isError ? 'failed' : 'completed';
        log.write(result.isError ? 'WARN' : 'INFO', 'tool_' + outcome,
          `${request.tool} ${outcome} in ${durationMs} ms; result reported to Sonne.`,
          {...request, durationMs});
      } catch (error) {
        const durationMs = Math.round(performance.now() - started);
        const reason = connectionDetail(error);
        log.write('ERROR', 'tool_failed', `${request.tool} failed during ${stage} after ${durationMs} ms: ${reason}.`,
          {...request, durationMs, stage, reason});
        throw error;
      }
    }
    await pause(1000);
  } catch (error) {
    const reason = connectionDetail(error);
    if (lastConnectionError !== reason) log.write('WARN', 'connection_retry', 'Revit connection: ' + reason + '. Retrying.', {reason});
    lastConnectionError = reason;
    sessionId = undefined;
    await pause(3000);
  }
}
