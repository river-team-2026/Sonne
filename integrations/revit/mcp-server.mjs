import {Server} from '@modelcontextprotocol/sdk/server/index.js';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {ListToolsRequestSchema, CallToolRequestSchema} from '@modelcontextprotocol/sdk/types.js';
import {api, readConfig, READ_TOOLS, WRITE_TOOLS} from './common.mjs';
import {fileURLToPath} from 'node:url';
import {createHash, randomBytes} from 'node:crypto';

const config = await readConfig(process.argv[2] || fileURLToPath(new URL('./server.json', import.meta.url)));
const text = value => ({content: [{type: 'text', text: JSON.stringify(value)}]});
const targetId = session => 'rvt-' + createHash('sha256').update(session.id + '\0' + (session.model?.documentSessionId || '')).digest('hex').slice(0, 12);
const targetProperty = {type: 'string', description: 'Exact short target_id from revit_sessions, e.g. rvt-012345abcdef. Copy it; never invent it.'};
const annotations = {readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true};
const inspections = new Map();
const audits = new Map();
function rememberAudit(result, session) {
  if (result.isError) return result;
  const data = JSON.parse(result.content[0].text);
  for (const [id, snapshot] of audits) if (Date.now() - snapshot.at > 300000) audits.delete(id);
  if (audits.size >= 64) audits.delete(audits.keys().next().value);
  if (data.audit_id) audits.set(data.audit_id, {at: Date.now(), target: targetId(session)});
  return result;
}
async function target(args, catalogOnly = false) {
  const {sessions} = await api(config, 'sessions');
  const session = sessions.find(s => args.target_id ? targetId(s) === args.target_id : s.id === args.session_id && (catalogOnly || s.model?.documentSessionId === args.model_id));
  if (!session) throw new Error('Target is missing or stale. Call revit_sessions and copy its current target_id; this is not evidence that Revit is offline.');
  return session;
}
const server = new Server({name: 'sonne-revit', version: '0.1.0'}, {capabilities: {tools: {}}});
server.setRequestHandler(ListToolsRequestSchema, async () => ({tools: [
  {name: 'revit_sessions', description: 'List LIVE clients and active Revit documents; copy target_id. For parameter checks, missing comments or bulk changes NEXT call revit_audit, then revit_bulk_set if requested. For other model reads use revit_read. No RVT upload needed.', inputSchema: {type: 'object', properties: {}, additionalProperties: false}},
  {name: 'revit_room_publications', description: 'Check publication history, area changes and brief discrepancies for the approved room schedule. Omit version for the latest two reports or supply it for one complete report and snapshot. Includes Discord alert status.', inputSchema: {type: 'object', properties: {version: {type: 'integer', minimum: 1, maximum: 999999999999999}}, additionalProperties: false}},
  {name: 'revit_tools', description: 'Get MCP argument schemas for the selected target. Supply tool to fetch one schema, avoiding the entire catalog.', inputSchema: {type: 'object', properties: {target_id: targetProperty, tool: {type: 'string'}}, required: ['target_id'], additionalProperties: false}},
  {name: 'revit_audit', description: 'GOTOWA PROCEDURA: sprawdź parametry, znajdź braki, uzupełnij brakujące komentarze, przygotuj zbiorczą zmianę. Dla zaznaczonych elementów wystarczy target_id i parameter ALL_MODEL_INSTANCE_COMMENTS: scope selection i rule empty są domyślne. Zaznaczenie czyta sama procedura; nie wywołuj get_current_view_elements ani get_selected_elements. Zwraca liczniki i audit_id do revit_bulk_set. Read-only complete parameter audit; use rule all for every writable value, empty for blanks. Reports missing/ambiguous/read-only parameters. No C# needed.', inputSchema: {type: 'object', properties: {
    target_id: targetProperty, parameter: {type: 'string', minLength: 1, description: 'Komentarze / Comments = ALL_MODEL_INSTANCE_COMMENTS. Oznaczenie / Mark = ALL_MODEL_MARK. Also accepts an exact localized name, numeric parameter ID or shared GUID.'}, scope: {type: 'string', enum: ['selection', 'category', 'model', 'elements'], default: 'selection', description: 'selection = zaznaczone elementy; category = wskazana kategoria; model = cały model; elements = jawne ID.'},
    category: {type: 'string', description: 'Required for category scope, e.g. OST_DuctCurves.'}, element_ids: {type: 'array', items: {type: 'string'}, minItems: 1, maxItems: 10000, description: 'Required for elements scope; exact UniqueIds or numeric IDs.'},
    rule: {type: 'string', enum: ['empty', 'equals', 'not_equals', 'all'], default: 'empty'}, expected: {type: ['string', 'number'], description: 'Required for equals/not_equals. Use text with units for Double parameters.'}
  }, required: ['target_id', 'parameter'], additionalProperties: false}},
  {name: 'revit_bulk_set', annotations: {...annotations, readOnlyHint: false, destructiveHint: true, idempotentHint: false}, description: 'GOTOWA PROCEDURA: uzupełnij lub zmień parametr we wszystkich editable_matching z revit_audit. Podaj tylko audit_id oraz nową value. Dla uzupełniania braków poprzedź audytem rule empty; reszta wartości zostaje zachowana. Revit verifies prior values, executes one atomic transaction and reads back all results. Single use, 5-minute expiry; stale data rejects the whole batch. No retry/save/sync or C#.', inputSchema: {type: 'object', properties: {audit_id: {type: 'string', description: 'Copy the audit_id from revit_audit, e.g. aud-012345abcdef.'}, value: {type: ['string', 'number'], description: 'NOWA wartość podana przez użytkownika. Text, integer ID/number, or text with units for Double.'}}, required: ['audit_id', 'value'], additionalProperties: false}},
  {name: 'revit_read', description: 'Read LIVE model content. Use get_current_view_info for the drawing; ai_element_filter for elements; sonne_get_element with arguments {element_unique_id} to inspect an element and receive its inspection_id for editing. Optionally add parameter_ids:[id] to limit the result. No file upload is required.', inputSchema: {type: 'object', properties: {target_id: targetProperty, tool: {type: 'string', enum: READ_TOOLS}, arguments: {type: 'object', additionalProperties: true}}, required: ['target_id', 'tool'], additionalProperties: false}},
  {name: 'revit_edit', annotations: {...annotations, readOnlyHint: false, destructiveHint: true, idempotentHint: false}, description: 'Edit the element from a fresh sonne_get_element inspection. Copy inspection_id from that result. For set_parameter, provide the parameter_id and NEW value only; the server checks the previous value automatically. For move, provide translation_mm. Double values are text with units. Then read back. No automatic retry, save or ACC synchronization.', inputSchema: {type: 'object', properties: {
    inspection_id: {type: 'string', description: 'Copy inspection_id from sonne_get_element. Single use, valid for 5 minutes.'}, operation: {type: 'string', enum: ['set_parameter', 'move']},
    parameter_id: {type: 'string'}, value: {type: ['string', 'number']},
    translation_mm: {type: 'array', items: {type: 'number'}, minItems: 3, maxItems: 3}
  }, required: ['inspection_id', 'operation'], additionalProperties: false}},
  {name: 'revit_call', annotations: {...annotations, readOnlyHint: false, destructiveHint: true, idempotentHint: false}, description: 'Call the FULL upstream MCP: both reads (including sonne_get_element) and creation/deletion, grids, levels, rooms, dimensions, tags, view operations, database writes and C#. The server determines read/write permissions automatically. First fetch the exact tool schema using revit_tools. Use revit_edit for inspected parameter/move edits. No automatic retry. C# runs with Revit process permissions, not in a sandbox. Do not save/sync unless requested. Verify edits with sonne_get_element.', inputSchema: {type: 'object', properties: {target_id: targetProperty, tool: {type: 'string', enum: [...READ_TOOLS, ...WRITE_TOOLS.filter(name => name !== 'sonne_edit_element')]}, arguments: {type: 'object', description: 'Actual JSON object matching the selected tool schema, never a quoted JSON string. For create_level: {"data":[{"name":"user requested name","elevation":0,"createFloorPlan":false,"createCeilingPlan":false}]}. Put name inside each data item; description does not replace name.', additionalProperties: true}}, required: ['target_id', 'tool', 'arguments'], additionalProperties: false}},
].map(tool => ({...tool, annotations: tool.annotations || annotations}))}));
server.setRequestHandler(CallToolRequestSchema, async ({params}) => {
  try {
    let args = params.arguments || {};
    if (params.name === 'revit_sessions') {
      const {sessions} = await api(config, 'sessions');
      return text({sessions: sessions.map(({tools, ...session}) => ({...session, target_id: targetId(session), tools: tools.map(tool => tool.name)}))});
    }
    if (params.name === 'revit_room_publications') {
      if (!config.publicationUrl) throw new Error('Room publication history has not been enabled.');
      const publicationConfig = {url: config.publicationUrl, token: config.token};
      if (args.version !== undefined) {
        if (!Number.isSafeInteger(args.version) || args.version < 1 || args.version > 999999999999999)
          throw new Error('Choose a positive integer publication version.');
        return text(await api(publicationConfig, 'publications/' + args.version));
      }
      const history = await api(publicationConfig, 'publications');
      return text({...history, publications: history.publications.slice(0, 2)});
    }
    if (params.name === 'revit_tools') {
      const session = await target(args, true);
      const tools = args.tool ? session.tools.filter(tool => tool.name === args.tool) : session.tools;
      if (!tools.length) throw new Error('Tool is not available for this target. List sessions for the enabled catalog.');
      return text(tools);
    }
    if (params.name === 'revit_audit') {
      const session = await target(args);
      const {target_id, ...audit} = args;
      return rememberAudit(await api(config, 'call', {sessionId: session.id, modelId: session.model.documentSessionId, name: 'sonne_audit_parameters', kind: 'read', arguments: audit}), session);
    }
    if (params.name === 'revit_bulk_set') {
      const snapshot = audits.get(args.audit_id);
      if (!snapshot || Date.now() - snapshot.at > 300000) throw new Error('Audit expired or already used. Run revit_audit again.');
      audits.delete(args.audit_id);
      const session = await target({target_id: snapshot.target});
      return await api(config, 'call', {sessionId: session.id, modelId: session.model.documentSessionId, name: 'sonne_bulk_set_parameters', kind: 'write', arguments: {audit_id: args.audit_id, value: args.value}});
    }
    if (params.name === 'revit_call') {
      if (args.tool === 'sonne_edit_element') throw new Error('Use revit_edit for inspected parameter/move edits.');
      if (WRITE_TOOLS.includes(args.tool)) {
        const session = await target(args);
        return await api(config, 'call', {sessionId: session.id, modelId: session.model.documentSessionId, name: args.tool, kind: 'write', arguments: args.arguments || {}});
      }
    }
    if (params.name === 'revit_edit') {
      if (!args.inspection_id) throw new Error('Read sonne_get_element first and supply its inspection_id.');
      if (args.inspection_id) {
        const snapshot = inspections.get(args.inspection_id);
        if (!snapshot || Date.now() - snapshot.at > 300000) throw new Error('Inspection expired or already used. Read sonne_get_element again.');
        const parameter = snapshot.data.parameters.find(p => p.id === args.parameter_id);
        if (args.operation === 'set_parameter' && (!parameter || parameter.readOnly)) throw new Error('Select a writable parameter from this inspection.');
        args = {...args, target_id: snapshot.target, element_unique_id: snapshot.data.uniqueId,
          ...(args.operation === 'set_parameter' ? {expected_value: parameter?.value} : {expected_anchor_mm: snapshot.data.anchor_mm})};
        inspections.delete(args.inspection_id);
      }
      if (!args.element_unique_id || !['set_parameter', 'move'].includes(args.operation)) throw new Error('Choose a supported edit and an exact element UniqueId.');
      if (args.operation === 'set_parameter' && (!args.parameter_id || !Object.hasOwn(args, 'expected_value') || !Object.hasOwn(args, 'value'))) throw new Error('Inspect the parameter first; parameter_id, expected_value and value are required.');
      if (args.operation === 'move' && [args.expected_anchor_mm, args.translation_mm].some(v => !Array.isArray(v) || v.length !== 3 || v.some(n => !Number.isFinite(n)))) throw new Error('Expected and translation coordinates must be three numbers in millimetres.');
      const session = await target(args);
      const {session_id, model_id, target_id, inspection_id, ...edit} = args;
      return await api(config, 'call', {sessionId: session.id, modelId: session.model.documentSessionId, name: 'sonne_edit_element', kind: 'write', arguments: edit});
    }
    if (!['revit_read', 'revit_call'].includes(params.name) || !READ_TOOLS.includes(args.tool)) throw new Error('Unknown read tool.');
    const session = await target(args);
    const result = await api(config, 'call', {sessionId: session.id, modelId: session.model.documentSessionId, name: args.tool, kind: 'read', arguments: args.arguments || {}});
    if (args.tool === 'sonne_audit_parameters') return rememberAudit(result, session);
    if (args.tool === 'sonne_get_element' && !result.isError) {
      const data = JSON.parse(result.content[0].text);
      if (Array.isArray(args.arguments?.parameter_ids)) data.parameters = data.parameters.filter(p => args.arguments.parameter_ids.includes(p.id));
      for (const [id, snapshot] of inspections) if (Date.now() - snapshot.at > 300000) inspections.delete(id);
      if (inspections.size >= 128) inspections.delete(inspections.keys().next().value);
      const inspection = 'ins-' + randomBytes(6).toString('hex');
      inspections.set(inspection, {at: Date.now(), target: targetId(session), data});
      data.inspection_id = inspection;
      return text(data);
    }
    return result;
  } catch (error) {return {isError: true, content: [{type: 'text', text: (['revit_edit', 'revit_call', 'revit_bulk_set'].includes(params.name) ? 'Edit outcome not confirmed. Inspect before retrying. ' : '') + error.message}]};}
});
await server.connect(new StdioServerTransport());
