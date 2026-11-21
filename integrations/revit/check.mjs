// Run inside the OpenClaw integration directory to check the actual MCP path.
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {fileURLToPath} from 'node:url';

const mcp = new Client({name: 'sonne-revit-check', version: '0.1.0'});
try {
  await mcp.connect(new StdioClientTransport({command: process.execPath, args: [fileURLToPath(new URL('./mcp-server.mjs', import.meta.url))], stderr: 'inherit'}));
  const listing = await mcp.callTool({name: 'revit_sessions', arguments: {}});
  if (listing.isError) throw new Error(listing.content[0].text);
  const {sessions} = JSON.parse(listing.content[0].text);
  console.log(JSON.stringify({sessions: process.argv.includes('--brief') ? sessions.map(({target_id, client, model, writeEnabled, tools}) => ({target_id, client, model, writeEnabled, tools: tools.map(tool => typeof tool === 'string' ? tool : tool.name)})) : sessions}, null, 2));
  if (process.argv.includes('--read')) {
    const available = sessions.filter(session => session.model?.documentSessionId);
    if (available.length !== 1) throw new Error('Open exactly one test model/client for this diagnostic.');
    const session = available[0];
    const result = await mcp.callTool({name: 'revit_read', arguments: {session_id: session.id, model_id: session.model.documentSessionId, tool: 'get_current_view_info', arguments: {}}});
    console.log(JSON.stringify(result, null, 2));
    if (result.isError) process.exitCode = 1;
  }
} catch (error) {console.error(error.message); process.exitCode = 1;}
finally {await mcp.close();}
