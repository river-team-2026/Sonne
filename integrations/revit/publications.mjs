import {createHash, randomUUID} from 'node:crypto';
import {createWriteStream, createReadStream, constants} from 'node:fs';
import {link, mkdir, open, rename, unlink, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
import {Transform} from 'node:stream';
import {pipeline} from 'node:stream/promises';
import {DatabaseSync} from 'node:sqlite';

export const maxArchiveBytes = 512 * 1024 * 1024;
const units = value => Math.round(value * 1e6); // Square millimetres for stable comparisons.
export function publicationError(message, status = 400) {
  return Object.assign(new Error(message), {status});
}
const safeText = (value, max = 160) => typeof value === 'string' && value.length <= max && !/[\u0000-\u001f]/.test(value);
const validArea = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1e7;

export function checkConfig(config) {
  if (!['local', 'aps'].includes(config.source) || !safeText(config.model_key) || !config.model_key ||
      !safeText(config.schedule_id, 128) || !config.schedule_id || !safeText(config.model_label) || !config.model_label)
    throw publicationError('Configure one source, model and approved schedule UniqueId.');
  if (!config.brief || !safeText(config.brief.revision) || !config.brief.revision ||
      !validArea(config.brief.tolerance_m2) || !Array.isArray(config.brief.rooms) || !config.brief.rooms.length || config.brief.rooms.length > 200)
    throw publicationError('Configure the brief revision, area tolerance and room requirements.');
  const keys = new Set();
  for (const row of config.brief.rooms) {
    if (!safeText(row?.number, 80) || !row.number || row.number.trim() !== row.number ||
        !validArea(row.area_m2) || row.area_m2 <= 0 || keys.has(row.number) ||
        (row.name !== undefined && (!safeText(row.name) || !row.name.trim())))
      throw publicationError('Brief room numbers must be unique, with a positive numeric area in m².');
    keys.add(row.number);
  }
  return config;
}

export function publicationBrief(config, prompt) {
  const section = prompt.split(/^## Project brief\r?$/m)[1]?.split(/^## /m)[0] ?? '';
  const lines = section.split(/\r?\n/).map(line => line.trim()).filter(line => /^Room check\b/i.test(line));
  if (!lines.length) return config.brief;
  const rooms = lines.map(line => {
    const match = /^Room check\s*\|\s*([^|]+)\|\s*(\d+(?:\.\d+)?)\s*(?:\|\s*([^|]+))?$/i.exec(line);
    if (!match) throw publicationError('In Project brief use: Room check | number | area in m² | optional name. Apply the correction to Sonne.');
    return {number: match[1].trim(), area_m2: Number(match[2]), ...(match[3] && {name: match[3].trim()})};
  });
  const revision = 'Studio-' + createHash('sha256').update(JSON.stringify(rooms)).digest('hex').slice(0, 12);
  return checkConfig({...config, brief: {...config.brief, revision, rooms}}).brief;
}

export function validateExtraction(data, scheduleId, digest) {
  if (data?.schedule_id !== scheduleId || data.digest !== digest || !safeText(data.schedule_name) ||
      !data.schedule_name.endsWith('-Sonne') || !Array.isArray(data.rooms) || data.rooms.length > 200)
    throw publicationError('Revit returned data for another archive or schedule.', 502);
  const numbers = new Set(), ids = new Set();
  return data.rooms.map(row => {
    if (!safeText(row?.id, 128) || !row.id || ids.has(row.id) || !safeText(row.number, 80) ||
        !row.number || row.number.trim() !== row.number || numbers.has(row.number) || !safeText(row.name) ||
        !(row.area_m2 === null || (validArea(row.area_m2) && row.area_m2 > 0)))
      throw publicationError('The approved schedule contains ambiguous room numbers or invalid room data.', 422);
    numbers.add(row.number); ids.add(row.id);
    return {id: row.id, number: row.number, name: row.name, area_m2: row.area_m2 === null ? null : units(row.area_m2) / 1e6};
  }).sort((a,b) => a.number.localeCompare(b.number));
}

export function compare(previous, current, brief) {
  const before = new Map((previous?.rooms || []).map(room => [room.id, room]));
  const now = new Map(current.rooms.map(room => [room.id, room]));
  const byNumber = new Map(current.rooms.map(room => [room.number, room]));
  const added = previous ? current.rooms.filter(room => !before.has(room.id)) : [];
  const removed = previous ? previous.rooms.filter(room => !now.has(room.id)) : [];
  const changed = current.rooms.flatMap(after => {
    const old = before.get(after.id);
    if (!old || (old.number === after.number && old.name === after.name && old.area_m2 === after.area_m2)) return [];
    return [{before: old, after, delta_m2: old.area_m2 === null || after.area_m2 === null ? null : (units(after.area_m2) - units(old.area_m2)) / 1e6}];
  });
  const discrepancies = [], unchecked = [];
  for (const target of brief.rooms) {
    const room = byNumber.get(target.number);
    if (room && target.name && room.name !== target.name)
      discrepancies.push({kind: 'name', number: target.number, expected: target.name, actual: room.name});
    if (!room || room.area_m2 === null) {
      unchecked.push({number: target.number, reason: room ? 'no_area' : 'not_in_schedule'});
      discrepancies.push({number: target.number, kind: room ? 'no_area' : 'not_in_schedule'});
      continue;
    }
    const delta = units(room.area_m2) - units(target.area_m2);
    if (Math.abs(delta) > units(brief.tolerance_m2))
      discrepancies.push({number: room.number, name: room.name, model_m2: room.area_m2, brief_m2: target.area_m2, delta_m2: delta / 1e6});
  }
  return {previous_version: previous?.version ?? null, version: current.version, schedule: current.schedule_name,
    brief_revision: brief.revision, tolerance_m2: brief.tolerance_m2, rooms: current.rooms.length,
    added, removed, changed, discrepancies, unchecked};
}

export function compact(report) {
  if (!report) return null;
  const {added, removed, changed, discrepancies, unchecked, ...rest} = report;
  return {...rest, counts: {added: added.length, removed: removed.length, changed: changed.length,
    discrepancies: discrepancies.length, unchecked: unchecked.length},
    changed: changed.slice(0, 8), discrepancies: discrepancies.slice(0, 8), unchecked: unchecked.slice(0, 8)};
}

export async function atomicJson(path, data) {
  const temporary = path + '.' + randomUUID() + '.tmp';
  try {
    await writeFile(temporary, JSON.stringify(data, null, 2) + '\n', {flag: 'wx', mode: 0o600});
    await rename(temporary, path);
  } finally {await unlink(temporary).catch(() => {});}
}

export async function archive(stream, directory, expectedSize) {
  await mkdir(directory, {recursive: true});
  const temporary = join(directory, randomUUID() + '.tmp');
  const hash = createHash('sha256');
  let size = 0, signature = Buffer.alloc(0);
  try {
    await pipeline(stream, new Transform({transform(chunk, encoding, done) {
      size += chunk.length;
      if (size > maxArchiveBytes) return done(publicationError('RVT archive exceeds 512 MiB.', 413));
      if (signature.length < 8) signature = Buffer.concat([signature, chunk.subarray(0, 8 - signature.length)]);
      hash.update(chunk); done(null, chunk);
    }}), createWriteStream(temporary, {flags: 'wx', flush: true, mode: 0o600}));
    if (!signature.equals(Buffer.from('d0cf11e0a1b11ae1', 'hex')) || (expectedSize !== undefined && size !== expectedSize))
      throw publicationError('Expected a complete, standalone Revit RVT file.', 422);
    const digest = hash.digest('hex');
    // A hard link publishes the immutable bytes without ever replacing an existing digest.
    await link(temporary, join(directory, digest + '.rvt')).catch(error => {if (error.code !== 'EEXIST') throw error;});
    if (process.platform !== 'win32') {
      const folder = await open(directory, 'r');
      try {await folder.sync();} finally {await folder.close();}
    }
    return digest;
  } finally {await unlink(temporary).catch(() => {});}
}

export async function archiveLocal(file, inbox, directory) {
  if (typeof file !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,150}\.rvt$/i.test(file) || file.includes('..'))
    throw publicationError('Use a saved .rvt basename from the private publication inbox.');
  const path = join(inbox, file);
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0)).catch(() => null);
  if (!handle) throw publicationError('Saved RVT not found in the publication inbox.', 404);
  try {
    const before = await handle.stat();
    if (!before.isFile() || before.size > maxArchiveBytes) throw publicationError('Expected a regular RVT file at most 512 MiB.');
    const digest = await archive(createReadStream(path, {fd: handle.fd, autoClose: false}), directory, before.size);
    const after = await handle.stat();
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs) throw publicationError('The RVT changed during publication. Finish saving before retrying.', 409);
    return digest;
  } finally {await handle.close();}
}

export function openHistory(path, config) {
  const db = new DatabaseSync(path);
  db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS publications (
      version INTEGER PRIMARY KEY, reference TEXT NOT NULL, digest TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      snapshot TEXT, report TEXT, message_id TEXT, first_send INTEGER, last_error TEXT
    );`);
  const identity = JSON.stringify([config.source, config.model_key, config.schedule_id, config.aps?.project_id, config.aps?.lineage_urn]);
  db.prepare("INSERT OR IGNORE INTO settings VALUES ('identity', ?)").run(identity);
  if (db.prepare("SELECT value FROM settings WHERE key='identity'").get().value !== identity) {
    db.close();
    throw publicationError('Use a new history directory for a different model or schedule.', 409);
  }
  const get = version => db.prepare('SELECT * FROM publications WHERE version=?').get(version);
  return {
    close: () => db.close(),
    get,
    rows: () => db.prepare('SELECT version, reference, digest, created_at, report, message_id, first_send, last_error FROM publications ORDER BY version DESC LIMIT 20').all(),
    nextAlert: since => db.prepare("SELECT * FROM publications WHERE message_id IS NULL AND (first_send IS NULL OR first_send > ?) AND json_array_length(report, '$.discrepancies') > 0 ORDER BY version LIMIT 1").get(since),
    scanUntil: () => Number(db.prepare("SELECT value FROM settings WHERE key='scan_until'").get()?.value ?? 0),
    requestScan: until => db.prepare("INSERT INTO settings VALUES ('scan_until', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(String(until)),
    pending: () => db.prepare('SELECT * FROM publications WHERE snapshot IS NULL ORDER BY version LIMIT 1').get(),
    previous: version => db.prepare('SELECT * FROM publications WHERE version < ? AND snapshot IS NOT NULL ORDER BY version DESC LIMIT 1').get(version),
    enqueue(version, reference, digest = null) {
      if (!Number.isSafeInteger(version) || version < 1) throw publicationError('Use a positive integer publication version.');
      const current = get(version);
      if (current) {
        if ((digest && current.digest && current.digest !== digest) || (!digest && current.reference !== reference))
          throw publicationError('That version already refers to different source data.', 409);
        return false;
      }
      const latest = db.prepare('SELECT MAX(version) AS version FROM publications').get().version;
      if (latest !== null && version < latest) throw publicationError('An older publication cannot be inserted after a newer one was queued.', 409);
      db.prepare('INSERT INTO publications(version,reference,digest) VALUES (?,?,?)').run(version, reference, digest);
      return true;
    },
    digest: (version, digest) => db.prepare('UPDATE publications SET digest=? WHERE version=? AND digest IS NULL').run(digest, version),
    capture(version, snapshot, report) {
      db.prepare('UPDATE publications SET snapshot=?, report=?, last_error=NULL WHERE version=? AND snapshot IS NULL')
        .run(JSON.stringify(snapshot), JSON.stringify(report), version);
    },
    error: (version, message) => db.prepare('UPDATE publications SET last_error=? WHERE version=?').run(message, version),
    trying: version => db.prepare('UPDATE publications SET first_send=COALESCE(first_send,?) WHERE version=?').run(Date.now(), version),
    delivered: (version, id) => db.prepare('UPDATE publications SET message_id=?, last_error=NULL WHERE version=?').run(id, version)
  };
}

export function discordMessage(report, config) {
  const numbers = new Intl.NumberFormat('en-US', {maximumFractionDigits: 6});
  const area = value => numbers.format(value) + ' m²';
  const items = [];
  for (const item of report.discrepancies.slice(0, 6)) {
    let detail;
    if (item.kind === 'name') detail = 'expected name "' + item.expected + '", found "' + item.actual + '".';
    else if (item.kind === 'no_area') detail = 'required room has no measurable area.';
    else if (item.kind === 'not_in_schedule') detail = 'required room is missing from the schedule.';
    else detail = 'model ' + area(item.model_m2) + ', brief ' + area(item.brief_m2) +
      ', difference ' + (item.delta_m2 > 0 ? '+' : '') + area(item.delta_m2) + '.';
    const line = '• Room ' + item.number + ' — ' + detail;
    if (items.join('\n').length + line.length > 1350) break;
    items.push(line);
  }
  const rest = report.discrepancies.length - items.length;
  return (config.source === 'local' ? '[LOCAL TEST] ' : '') + 'Sonne room brief discrepancies\n' +
    config.model_label + ' · version ' + report.version +
    (report.previous_version ? ' (previous version ' + report.previous_version + ')' : '') +
    ' · brief ' + report.brief_revision + ' · tolerance ±' + area(report.tolerance_m2) + '\n' +
    items.join('\n') + (rest ? '\nAdditional mismatches: ' + rest + '.' : '') +
    '\nConfirm these discrepancies with the project team.';
}
