import {z} from "zod";
import {McpServer} from "@modelcontextprotocol/sdk/server/mcp.js";
import {withRevitConnection} from "../utils/ConnectionManager.js";

export function registerSonneElementTools(server: McpServer) {
  const target = {
    element_unique_id: z.string().min(1),
    _sonneModelId: z.string().min(1),
    _sonneDeadline: z.number(),
    _sonneOperationId: z.string().optional(),
  };
  const call = async (name: string, args: object) => {
    try {
      const value = await withRevitConnection(revit => revit.sendCommand(name, args));
      return {content: [{type: "text" as const, text: JSON.stringify(value)}]};
    } catch (error) {return {isError: true, content: [{type: "text" as const, text: error instanceof Error ? error.message : String(error)}]};}
  };
  server.tool("sonne_get_element", "Read one element's parameters, raw values, parameter IDs and anchor in millimetres. Use its UniqueId from ai_element_filter.", target, args => call("sonne_get_element", args));
  server.tool("sonne_edit_element", "Edit one element in a Revit transaction after checking the expected document and previous value/position. No save or sync.", {
    ...target,
    operation: z.enum(["set_parameter", "move"]),
    parameter_id: z.string().optional(),
    expected_value: z.union([z.string(), z.number(), z.null()]).optional(),
    value: z.union([z.string(), z.number()]).optional(),
    expected_anchor_mm: z.tuple([z.number(), z.number(), z.number()]).optional(),
    translation_mm: z.tuple([z.number(), z.number(), z.number()]).optional(),
  }, args => call("sonne_edit_element", args));
}
