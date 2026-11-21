import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHub} from '../hub.mjs';
import {api, ROOM_PUBLICATION_TOOL} from '../common.mjs';

test('authenticated routing keeps client sessions separate and refuses writes and stale documents', async t => {
  const serverToken = 'server-'.padEnd(48, 's'), aToken = 'client-a-'.padEnd(48, 'a'), bToken = 'client-b-'.padEnd(48, 'b');
  const server = createHub({serverToken, clients: [{id: 'a', label: 'Architect', token: aToken}, {id: 'b', label: 'Engineer', token: bToken}]});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {server.closeAllConnections(); server.close();});
  const url = `http://127.0.0.1:${server.address().port}/`;
  const operator = {url, token: serverToken}, a = {url, token: aToken}, b = {url, token: bToken};
  const tools = [{name: 'get_current_view_info', inputSchema: {type: 'object'}}, {name: 'send_code_to_revit', inputSchema: {type: 'object'}}];
  const {sessionId: aId} = await api(a, 'register', {tools, model: {documentSessionId: 'model-a'}});
  const {sessionId: bId} = await api(b, 'register', {tools, model: {documentSessionId: 'model-b'}});
  assert.equal((await api(operator, 'sessions')).sessions.length, 2);
  await assert.rejects(api(a, 'sessions'), /Invalid client session/);
  await assert.rejects(api({url, token: 'wrong'}, 'sessions'), /Unauthorized/);
  await assert.rejects(api(operator, 'call', {sessionId: aId, modelId: 'model-a', name: 'send_code_to_revit'}), /not enabled/);
  await assert.rejects(api(operator, 'call', {sessionId: aId, modelId: 'model-b', name: 'get_current_view_info'}), /Select an open document/);
  const call = api(operator, 'call', {sessionId: aId, modelId: 'model-a', name: 'get_current_view_info'});
  const poll = await api(a, 'poll', {sessionId: aId, model: {documentSessionId: 'model-a'}});
  assert.equal(poll.job.name, 'get_current_view_info');
  assert.equal((await api(b, 'poll', {sessionId: bId, model: {documentSessionId: 'model-b'}})).job, null);
  await assert.rejects(api(b, 'result', {sessionId: bId, id: poll.job.id, result: {}}), /Invalid job owner/);
  const expected = {content: [{type: 'text', text: 'View from client A'}]};
  await api(a, 'result', {sessionId: aId, id: poll.job.id, result: expected});
  assert.deepEqual(await call, expected);
  const replacement = await api(a, 'register', {tools, model: {documentSessionId: 'model-a'}});
  assert.notEqual(replacement.sessionId, aId);
  await assert.rejects(api(a, 'poll', {sessionId: aId}), /Invalid client session/);
});

test('structured edits require server and client opt-in and cannot use the read route', async t => {
  const serverToken = 'server-'.padEnd(48, 's'), token = 'client-'.padEnd(48, 'c'), readToken = 'reader-'.padEnd(48, 'r');
  const server = createHub({serverToken, clients: [{id: 'editor', token, allowWrites: true}, {id: 'reader', token: readToken}]});
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {server.closeAllConnections(); server.close();});
  const url = `http://127.0.0.1:${server.address().port}/`;
  const operator = {url, token: serverToken}, device = {url, token}, reader = {url, token: readToken};
  const tools = [{name: 'sonne_edit_element', inputSchema: {type: 'object', properties: {_sonneModelId: {type: 'string'}, element_unique_id: {type: 'string'}}, required: ['_sonneModelId', 'element_unique_id']}}, ...['create_level', 'send_code_to_revit', 'sonne_bulk_set_parameters', 'sonne_audit_parameters'].map(name => ({name, inputSchema: {type: 'object'}}))];
  const model = {documentSessionId: 'document'};
  const disabled = await api(reader, 'register', {writeEnabled: true, tools, model});
  await assert.rejects(api(operator, 'call', {sessionId: disabled.sessionId, modelId: 'document', kind: 'write', name: 'sonne_edit_element'}), /not enabled/);
  const localDisabled = await api(device, 'register', {writeEnabled: false, tools, model});
  await assert.rejects(api(operator, 'call', {sessionId: localDisabled.sessionId, modelId: 'document', kind: 'write', name: 'sonne_edit_element'}), /not enabled/);
  for (const name of ['create_level', 'send_code_to_revit', 'sonne_bulk_set_parameters']) {
    await assert.rejects(api(operator, 'call', {sessionId: disabled.sessionId, modelId: 'document', kind: 'write', name}), /not enabled/);
    await assert.rejects(api(operator, 'call', {sessionId: localDisabled.sessionId, modelId: 'document', kind: 'write', name}), /not enabled/);
  }
  const {sessionId} = await api(device, 'register', {writeEnabled: true, tools, model});
  const session = (await api(operator, 'sessions')).sessions.find(s => s.id === sessionId);
  assert.equal(session.writeEnabled, true);
  assert.equal(session.tools.length, 5);
  const readSession = (await api(operator, 'sessions')).sessions.find(s => s.id === disabled.sessionId);
  assert.deepEqual(readSession.tools.map(t => t.name), ['sonne_audit_parameters'], 'Audits remain available without write permission');
  assert.deepEqual(session.tools[0].inputSchema.required, ['element_unique_id']);
  await assert.rejects(api(operator, 'call', {sessionId, modelId: 'document', kind: 'read', name: 'sonne_edit_element'}), /explicit write/);
  await assert.rejects(api(operator, 'call', {sessionId, modelId: 'old-document', kind: 'write', name: 'sonne_edit_element'}), /Select an open document/);
  const call = api(operator, 'call', {sessionId, modelId: 'document', kind: 'write', name: 'sonne_edit_element', arguments: {operation: 'set_parameter'}});
  const {job} = await api(device, 'poll', {sessionId, model});
  assert.equal(job.kind, 'write');
  assert.ok(job.expiresAt > Date.now());
  const result = {content: [{type: 'text', text: '{"status":"committed"}'}]};
  await api(device, 'result', {sessionId, id: job.id, result});
  assert.deepEqual(await call, result);
});

test('only the workstation holding a pending publication job can download its archive', async () => {
  const folder = await mkdtemp(join(tmpdir(), 'sonne-revit-archives-'));
  const serverToken = 'server'.padEnd(48, 's'), workerToken = 'worker'.padEnd(48, 'a'), otherToken = 'other'.padEnd(48, 'b');
  const server = createHub({serverToken, clients: [
    {id: 'worker', token: workerToken, allowPublications: true}, {id: 'other', token: otherToken}
  ]}, folder);
  const digest = 'd'.repeat(64);
  let pending;
  try {
    await writeFile(join(folder, digest + '.rvt'), 'archive bytes');
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const url = 'http://127.0.0.1:' + server.address().port + '/';
    const operator = {url, token: serverToken}, worker = {url, token: workerToken}, other = {url, token: otherToken};
    const tools = [{name: ROOM_PUBLICATION_TOOL, inputSchema: {type: 'object'}}];
    const model = {documentSessionId: 'document'};
    const {sessionId} = await api(worker, 'register', {model, tools});
    const unapproved = await api(other, 'register', {model, tools});
    const {sessions} = await api(operator, 'sessions');
    assert.equal(sessions.find(session => session.id === sessionId).publicationReader, true);
    assert.equal(sessions.find(session => session.id === unapproved.sessionId).publicationReader, false);
    assert.ok(sessions.every(session => !session.tools.some(tool => tool.name === ROOM_PUBLICATION_TOOL)));
    const getArchive = auth => fetch(url + 'archives/' + digest, {headers: {Authorization: 'Bearer ' + auth.token}});
    assert.equal((await getArchive(worker)).status, 403);
    const request = {sessionId, modelId: model.documentSessionId, name: ROOM_PUBLICATION_TOOL, kind: 'read',
      arguments: {digest, schedule_id: 'approved'}};
    await assert.rejects(api(operator, 'call', {...request, sessionId: unapproved.sessionId}), /not enabled/);
    pending = api(operator, 'call', request).catch(error => error);
    let job;
    for (let attempts = 0; !job && attempts < 20; attempts++) ({job} = await api(worker, 'poll', {sessionId, model}));
    assert.equal(job?.name, ROOM_PUBLICATION_TOOL);
    assert.equal((await getArchive(other)).status, 403);
    assert.equal((await getArchive(operator)).status, 403);
    const archive = await getArchive(worker);
    assert.equal(archive.status, 200);
    assert.equal(await archive.text(), 'archive bytes');
    const result = {content: [{type: 'text', text: 'extracted'}]};
    await api(worker, 'result', {sessionId, id: job.id, result});
    assert.deepEqual(await pending, result);
    assert.equal((await getArchive(worker)).status, 403);
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    await pending;
    await rm(folder, {recursive: true, force: true});
  }
});
