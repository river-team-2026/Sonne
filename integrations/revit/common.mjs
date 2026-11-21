import {readFile} from 'node:fs/promises';

// Classify the complete pinned upstream catalog. Unknown tools fail closed.
export const READ_TOOLS = ['get_current_view_info', 'get_current_view_elements', 'get_selected_elements', 'get_available_family_types', 'ai_element_filter', 'analyze_model_statistics', 'export_room_data', 'get_material_quantities', 'query_stored_data', 'sonne_get_element', 'sonne_audit_parameters'];
export const WRITE_TOOLS = ['color_elements', 'create_dimensions', 'create_grid', 'create_level', 'create_line_based_element', 'create_point_based_element', 'create_room', 'create_structural_framing_system', 'create_surface_based_element', 'delete_element', 'operate_element', 'say_hello', 'send_code_to_revit', 'store_project_data', 'store_room_data', 'tag_all_rooms', 'tag_all_walls', 'sonne_edit_element', 'sonne_bulk_set_parameters'];
export const enabledTools = writes => [...READ_TOOLS, ...(writes ? WRITE_TOOLS : [])];
// Internal extraction is queued by the publication service, never by the agent.
export const ROOM_PUBLICATION_TOOL = 'sonne_extract_room_publication';
export const ROOM_PUBLICATION_TIMEOUT = 115000;
export async function readConfig(file) {return JSON.parse(await readFile(file, 'utf8'));}
export async function api(config, route, body, timeout = 35000) {
  const response = await fetch(new URL(route, config.url.endsWith('/') ? config.url : config.url + '/'), {
    method: body === undefined ? 'GET' : 'POST',
    headers: {Authorization: 'Bearer ' + config.token, 'Content-Type': 'application/json'},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
    signal: AbortSignal.timeout(timeout), redirect: 'error',
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Bridge HTTP ${response.status}`);
  return result;
}
