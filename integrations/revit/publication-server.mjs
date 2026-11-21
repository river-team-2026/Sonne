import {createHash, timingSafeEqual} from 'node:crypto';
import {mkdirSync} from 'node:fs';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {api, readConfig, ROOM_PUBLICATION_TOOL, ROOM_PUBLICATION_TIMEOUT} from './common.mjs';
import {archiveLocal, checkConfig, compact, compare, discordMessage, openHistory, publicationBrief, publicationError, validateExtraction} from './publications.mjs';
import {acceptPublishHook, createApsSource, validateAps} from './publications-aps.mjs';

const alertWindow = 90000; // Keep ambiguous retries within Discord's nonce deduplication window.
const discordId = value => typeof value === 'string' && /^\d{17,20}$/.test(value);
const fail = publicationError;
const errorText = (error, fallback) => error.status ? error.message : fallback;

async function readBody(req) {
  const chunks = [];
  let bytes = 0;
  for await (const chunk of req) {
    bytes += chunk.length;
    if (bytes > 65536) throw fail('Publication notification exceeds 64 KiB.', 413);
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function reply(res, status, value) {
  res.writeHead(status, {'Content-Type': 'application/json', 'Cache-Control': 'no-store'});
  res.end(JSON.stringify(value));
}

function authorized(req, token) {
  const actual = Buffer.from(req.headers.authorization ?? '');
  const expected = Buffer.from('Bearer ' + token);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

function describe(row, guild, channel, full) {
  const report = row.report ? JSON.parse(row.report) : null;
  return {
    version: row.version, digest: row.digest, received_at_utc: row.created_at,
    status: report ? 'captured' : 'queued', ...(full && {snapshot: row.snapshot ? JSON.parse(row.snapshot) : null}),
    report: full ? report : compact(report),
    alert: {
      status: row.message_id ? 'sent' : !report ? 'waiting_for_snapshot' : !report.discrepancies.length ? 'not_needed' :
        row.first_send && Date.now() - row.first_send >= alertWindow ? 'delivery_unconfirmed' : 'pending',
      message_id: row.message_id,
      url: row.message_id && discordId(guild) && discordId(channel) ? 'https://discord.com/channels/' + guild + '/' + channel + '/' + row.message_id : null
    },
    error: row.last_error
  };
}

async function extractFromRevit(bridge, worker, row, schedule) {
  const {sessions} = await api(bridge, 'sessions');
  const session = sessions.find(item => item.client === worker && item.publicationReader && item.model?.documentSessionId);
  if (!session) throw fail('The designated Revit publication worker is offline or has no active document.', 503);
  const result = await api(bridge, 'call', {
    sessionId: session.id, modelId: session.model.documentSessionId, name: ROOM_PUBLICATION_TOOL, kind: 'read',
    arguments: {digest: row.digest, schedule_id: schedule}
  }, ROOM_PUBLICATION_TIMEOUT + 3000);
  if (result.isError) throw fail(String(result.content?.[0]?.text || 'Revit archive read failed.').slice(0, 300), 502);
  return JSON.parse(result.content[0].text);
}

function sendWithDiscord(discord, fetcher) {
  if (!discordId(discord.channel) || !discord.token) throw fail('Configure the existing Discord bot and target channel.');
  return async (content, nonce) => {
    const response = await fetcher('https://discord.com/api/v10/channels/' + discord.channel + '/messages', {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(12000),
      headers: {Authorization: 'Bot ' + discord.token, 'Content-Type': 'application/json'},
      body: JSON.stringify({content, nonce, enforce_nonce: true, allowed_mentions: {parse: []}})
    });
    if (!response.ok) throw fail('Discord delivery returned HTTP ' + response.status + '.', 502);
    return (await response.json()).id;
  };
}

export function createPublicationServer(config, {directory, bridge, discord, fetcher = fetch, extract, send, promptFile}) {
  checkConfig(config);
  if (!bridge?.token || bridge.token.length < 32 || !/^[\w-]{1,64}$/.test(config.worker ?? ''))
    throw fail('Configure the Revit router token and designated Windows client ID.');
  if (config.source === 'aps') validateAps(config.aps);
  const aps = config.source === 'aps' ? createApsSource(config.aps, directory, fetcher) : null;
  const archives = join(directory, 'archives'), inbox = join(directory, 'inbox');
  for (const path of [archives, inbox]) mkdirSync(path, {recursive: true, mode: 0o700});
  extract ??= (row, schedule) => extractFromRevit(bridge, config.worker, row, schedule);
  send ??= sendWithDiscord(discord, fetcher);
  const history = openHistory(join(directory, 'history.sqlite'), config);
  let running, nextScan = 0, nextCapture = 0, nextAlert = 0, scanError = null, acceptingLocal = false;

  async function sweepOnce() {
    if (aps && history.scanUntil() && Date.now() >= nextScan) {
      const until = history.scanUntil();
      try {
        for (const [version, urn] of await aps.versions()) history.enqueue(version, urn);
        scanError = null;
        if (Date.now() >= until && history.scanUntil() === until) history.requestScan(0);
      } catch (error) {scanError = errorText(error, 'Could not check for published versions in APS.');}
      nextScan = Date.now() + 15000;
    }

    let row = history.pending();
    if (row && Date.now() >= nextCapture) {
      try {
        if (!row.digest) {
          const digest = await aps.download(row, archives);
          history.digest(row.version, digest);
          row = {...row, digest};
        }
        const data = await extract(row, config.schedule_id);
        const prompt = promptFile ? await readFile(promptFile, 'utf8').catch(error => {
          if (error.code === 'ENOENT') return '';
          throw error;
        }) : '';
        const brief = publicationBrief(config, prompt);
        const snapshot = {version: row.version, digest: row.digest, schedule_id: config.schedule_id,
          schedule_name: data.schedule_name, rooms: validateExtraction(data, config.schedule_id, row.digest), brief};
        const before = history.previous(row.version);
        history.capture(row.version, snapshot, compare(before ? JSON.parse(before.snapshot) : null, snapshot, brief));
        nextCapture = 0;
      } catch (error) {
        history.error(row.version, errorText(error, 'Could not extract the saved RVT through Revit.'));
        nextCapture = Date.now() + 12000;
      }
    }

    row = history.nextAlert(Date.now() - alertWindow);
    if (row && Date.now() >= nextAlert) {
      const nonce = createHash('sha256').update(JSON.stringify([config.source, config.model_key, row.version, row.digest])).digest('hex').slice(0, 24);
      history.trying(row.version);
      try {
        const id = await send(discordMessage(JSON.parse(row.report), config), nonce);
        if (!discordId(id)) throw fail('Discord returned no valid message ID.', 502);
        history.delivered(row.version, id);
        nextAlert = 0;
      } catch (error) {
        history.error(row.version, errorText(error, 'Discord delivery could not be confirmed.'));
        nextAlert = Date.now() + 12000;
      }
    }
  }

  const sweep = () => running ??= sweepOnce().finally(() => {running = undefined;});
  const server = createServer({requestTimeout: 15000}, async (req, res) => {
    try {
      const path = new URL(req.url, 'http://localhost').pathname;
      if (req.method === 'GET' && path === '/health') return reply(res, 200, {ready: true});
      if (req.method === 'POST' && path === '/aps/publish' && aps) {
        if (!acceptPublishHook(await readBody(req), req.headers['x-adsk-signature'], config.aps))
          return reply(res, 202, {ignored: true});
        history.requestScan(Date.now() + 15 * 60000);
        return reply(res, 202, {queued: true});
      }
      if (!authorized(req, bridge.token)) throw fail('Unauthorized.', 401);
      const match = /^\/publications\/([1-9]\d{0,14})$/.exec(path);
      const state = row => describe(row, discord.guild, discord.channel, Boolean(match));
      const model = {source: config.source, model: config.model_label, schedule_id: config.schedule_id};
      if (req.method === 'GET' && path === '/publications')
        return reply(res, 200, {...model, ...(aps && {scan_error: scanError}), publications: history.rows().map(state)});
      if (req.method === 'GET' && match) {
        const row = history.get(Number(match[1]));
        if (!row) throw fail('Publication version not found.', 404);
        return reply(res, 200, {...model, publication: state(row)});
      }
      if (req.method === 'POST' && path === '/local/publish' && config.source === 'local') {
        let event;
        try {event = JSON.parse(await readBody(req));} catch (error) {throw error.status ? error : fail('Expected a publication JSON object.');}
        if (event?.event !== 'model.publish') return reply(res, 202, {ignored: true});
        if (!Number.isSafeInteger(event.version) || event.version < 1 || event.version > 999999999999999)
          throw fail('Use a positive integer publication version of at most 15 digits.');
        if (acceptingLocal) throw fail('An RVT is being archived. Retry after it finishes.', 409);
        acceptingLocal = true;
        try {
          const digest = await archiveLocal(event.file, inbox, archives);
          const queued = history.enqueue(event.version, event.file, digest);
          return reply(res, 202, {queued, version: event.version, digest});
        } finally {acceptingLocal = false;}
      }
      throw fail('Unknown publication endpoint.', 404);
    } catch (error) {
      if (!res.headersSent) reply(res, error.status || 500, {error: errorText(error, 'Publication service error.')});
    }
  });
  return {server, sweep, async close() {
    await new Promise(resolve => server.close(resolve));
    await running;
    history.close();
  }};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.umask(0o077);
  const directory = process.env.PUBLICATION_DIR || '/data';
  const config = await readConfig(join(directory, 'config.json'));
  const bridge = await readConfig(process.env.REVIT_SERVER_CONFIG || '/bridge/server.json');
  const discord = {token: process.env.DISCORD_BOT_TOKEN, channel: process.env.DISCORD_CHANNEL_ID, guild: process.env.DISCORD_GUILD_ID};
  const app = createPublicationServer(config, {directory, bridge, discord, promptFile: '/studio-brief/PROMPT'});
  const timer = setInterval(() => app.sweep().catch(() => console.error('Publication worker cycle failed.')), 1500);
  app.server.listen(Number(process.env.PORT || 9092), process.env.HOST || '127.0.0.1');
  process.once('SIGTERM', async () => {clearInterval(timer); await app.close();});
}
