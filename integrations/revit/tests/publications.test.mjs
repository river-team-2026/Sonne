import {createHash, createHmac} from 'node:crypto';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';
import {compare} from '../publications.mjs';
import {createPublicationServer} from '../publication-server.mjs';
import {acceptPublishHook, createApsSource} from '../publications-aps.mjs';

const config = {source: 'local', model_key: 'snowdon', model_label: 'Snowdon pilot',
  worker: 'windows', schedule_id: 'approved-uid',
  brief: {revision: 'TEST-01', tolerance_m2: 0.5, rooms: [{number: '101', area_m2: 60}, {number: '102', area_m2: 30}]}};
const room = (id, number, area_m2) => ({id, number, name: 'Test room', area_m2});
const rvt = suffix => Buffer.concat([Buffer.from('d0cf11e0a1b11ae1', 'hex'), Buffer.from(suffix)]);
const sha = value => createHash('sha256').update(value).digest('hex');

test('room diffs preserve identity and flag unchecked requirements independently of area mismatches', () => {
  const previous = {version: 1, rooms: [room('a', '101', 60), room('b', '102', 30), room('c', '103', 10)]};
  const current = {version: 2, rooms: [room('a', '101', 59.5), room('b', '102', null), room('d', '104', 12)]};
  const report = compare(previous, current, config.brief);
  assert.equal(report.previous_version, 1);
  assert.deepEqual(report.changed.map(entry => entry.delta_m2), [-0.5, null]);
  assert.deepEqual(report.added.map(entry => entry.id), ['d']);
  assert.deepEqual(report.removed.map(entry => entry.id), ['c']);
  assert.deepEqual(report.discrepancies, [{number: '102', kind: 'no_area'}]);
  assert.deepEqual(report.unchecked, [{number: '102', reason: 'no_area'}]);
});

test('local publish stores exact RVT bytes and sends at most one Discord alert across a restart', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'sonne-publications-'));
  const bridge = {token: 'test-operator'.padEnd(48, 'a')};
  const channel = '222222222222222222', guild = '111111111111111111', message = '333333333333333333';
  const alerts = [];
  const promptFile = join(directory, 'PROMPT');
  const options = {directory, bridge, promptFile, discord: {token: 'test-discord', channel, guild},
    extract: async row => ({
      digest: row.digest, schedule_id: 'approved-uid', schedule_name: 'Test rooms-Sonne',
      rooms: [room('a', '101', row.version === 1 ? 60 : 54), room('b', '102', 30)]
    }),
    fetcher: async (url, request) => {
      assert.equal(url, 'https://discord.com/api/v10/channels/' + channel + '/messages');
      assert.equal(request.method, 'POST');
      alerts.push(JSON.parse(request.body));
      return Response.json({id: message});
    }
  };
  let app, url;
  async function start() {
    app = createPublicationServer(config, options);
    await new Promise(resolve => app.server.listen(0, '127.0.0.1', resolve));
    url = 'http://127.0.0.1:' + app.server.address().port;
  }
  async function call(path, body) {
    const response = await fetch(url + path, {
      headers: {Authorization: 'Bearer ' + bridge.token},
      ...(body && {method: 'POST', body: JSON.stringify(body)})
    });
    return {status: response.status, body: await response.json()};
  }
  const publish = (version, file, event = 'model.publish') => call('/local/publish', {version, file, event});
  try {
    await start();
    await writeFile(join(directory, 'inbox', 'v1.rvt'), rvt('one'));
    await writeFile(join(directory, 'inbox', 'v2.rvt'), rvt('two'));
    assert.equal((await fetch(url + '/publications')).status, 401);
    assert.equal((await publish(1, '../other.rvt')).status, 400);
    assert.equal((await publish(1, 'v1.rvt', 'model.sync')).body.ignored, true);
    assert.equal((await call('/publications/1')).status, 404);
    assert.equal((await publish(1, 'v1.rvt')).body.digest, sha(rvt('one')));
    await writeFile(join(directory, 'inbox', 'v1.rvt'), rvt('modified after enqueue'));
    await app.sweep();
    assert.equal((await call('/publications/1')).body.publication.alert.status, 'not_needed');
    assert.equal(alerts.length, 0);
    await writeFile(promptFile, '## Project brief\nRoom check | 101 | 61 | Laboratory\nRoom check | 102 | 29\nRoom check | 103 | 10\n## Files\nRoom check | ignored | 999');
    assert.equal((await publish(2, 'v2.rvt')).body.queued, true);
    await app.sweep();
    const {publication} = (await call('/publications/2')).body;
    assert.equal(publication.report.previous_version, 1);
    assert.equal(publication.report.changed[0].delta_m2, -6);
    assert.deepEqual(publication.report.discrepancies.map(item => item.kind ?? item.delta_m2),
      ['name', -7, 1, 'not_in_schedule']);
    assert.match(publication.snapshot.brief.revision, /^Studio-/);
    assert.equal(publication.alert.message_id, message);
    assert.deepEqual(await readFile(join(directory, 'archives', sha(rvt('one')) + '.rvt')), rvt('one'));
    assert.match(alerts[0].content, /expected name "Laboratory"/);
    assert.match(alerts[0].content, /Room 103.*missing/);
    assert.match(alerts[0].nonce, /^[a-f0-9]{24}$/);
    assert.equal(alerts[0].enforce_nonce, true);
    assert.deepEqual(alerts[0].allowed_mentions.parse, []);
    await app.close();
    await start();
    assert.equal((await publish(2, 'v2.rvt')).body.queued, false);
    assert.equal((await publish(1, 'v1.rvt')).status, 409);
    await app.sweep();
    assert.equal(alerts.length, 1);
    assert.deepEqual((await call('/publications/2')).body.publication, publication);
  } finally {
    await app?.close();
    await rm(directory, {recursive: true, force: true});
  }
});

test('APS publish verifies the signature, lineage and exact published host before fetching the RVT', async () => {
  const aps = {project_id: 'b.559201f1-444a-4c5a-80fa-e2f851297d3b', lineage_urn: 'urn:adsk.wipprod:dm.lineage:Pilot',
    hook_id: 'pilot-hook', webhook_secret: 'test'.repeat(10), first_version: 2};
  const payload = {hook: {hookId: aps.hook_id, system: 'adsk.c4r', event: 'model.publish'}, payload: {ItemUrn: aps.lineage_urn}};
  const verify = value => {
    const raw = Buffer.from(JSON.stringify(value));
    const signature = 'sha1hash=' + createHmac('sha1', aps.webhook_secret).update(raw).digest('hex');
    assert.throws(() => acceptPublishHook(raw, signature.slice(0, -1), aps), /Invalid APS signature/);
    return acceptPublishHook(raw, signature, aps);
  };
  assert.equal(verify(payload), true);
  assert.equal(verify({...payload, payload: {ItemUrn: aps.lineage_urn + 'Other'}}), false);
  assert.equal(verify({...payload, hook: {...payload.hook, event: 'model.sync'}}), false);
  const directory = await mkdtemp(join(tmpdir(), 'sonne-aps-'));
  const versionId = n => 'urn:adsk.wipprod:fs.file:vf.Pilot?version=' + n;
  const wrong = {version: 3, reference: versionId(3)};
  const correct = {version: 2, reference: versionId(2)};
  const bytes = rvt('from the signed archive');
  const signed = 'https://example.s3.amazonaws.com/publish/host.rvt';
  let downloads = 0;
  try {
    await writeFile(join(directory, 'aps-auth.json'), JSON.stringify({client_id: 'fake-app', client_secret: 'fake-secret', refresh_token: 'fake-refresh'}));
    const source = createApsSource(aps, directory, async (path, req) => {
      const url = new URL(path);
      if (url.href === signed) {
        downloads++;
        assert.equal(req.headers, undefined, 'pre-signed S3 downloads do not receive the APS token');
        return new Response(bytes);
      }
      assert.equal(url.origin, 'https://developer.api.autodesk.com');
      if (url.pathname.endsWith('/token')) return Response.json({access_token: 'fake-access', expires_in: 3600, refresh_token: 'fake-new-refresh'});
      if (url.pathname.endsWith('/versions')) return Response.json({data: [3, 1, 2].map(versionNumber => ({
        id: versionId(versionNumber), attributes: {versionNumber, extension: {type: 'versions:autodesk.bim360:C4RModel'}}
      }))});
      assert.match(decodeURIComponent(url.pathname), /Pilot\?version=[23]\/linked-files$/);
      return Response.json({hostFile: {itemId: aps.lineage_urn, versionId: aps.lineage_urn + '?version=2', publishStatus: 'Published',
        modelName: 'host.rvt', size: bytes.length, signedUrl: signed}});
    });
    assert.deepEqual(await source.versions(), [[2,correct.reference], [3,wrong.reference]]);
    await assert.rejects(source.download(wrong, join(directory, 'archives')), /exact approved published RVT/);
    assert.equal(downloads, 0);
    assert.equal(await source.download(correct, join(directory, 'archives')), sha(bytes));
    assert.equal(downloads, 1);
    assert.equal(JSON.parse(await readFile(join(directory, 'aps-auth.json'), 'utf8')).refresh_token, 'fake-new-refresh');
  } finally {await rm(directory, {recursive: true, force: true});}
});
