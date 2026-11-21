import {z} from "zod";
import {McpServer} from "@modelcontextprotocol/sdk/server/mcp.js";
import {withRevitConnection} from "../utils/ConnectionManager.js";

export function registerRoomPublications(server: McpServer) {
  server.tool("sonne_extract_room_publication", "Internal reader for an authorized immutable RVT archive.", {
    digest: z.string().regex(/^[a-f0-9]{64}$/), schedule_id: z.string().min(1),
    _sonneModelId: z.string().min(1), _sonneDeadline: z.number(), _sonneOperationId: z.string().min(1)
  }, async args => {
    try {
      const data = await withRevitConnection(revit => revit.sendCommand("sonne_extract_room_publication", args));
      return {content: [{type: "text" as const, text: JSON.stringify(data)}]};
    } catch (error) {
      return {isError: true, content: [{type: "text" as const, text: error instanceof Error ? error.message : String(error)}]};
    }
  });
}
