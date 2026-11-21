import {z} from "zod";
import {McpServer} from "@modelcontextprotocol/sdk/server/mcp.js";
import {withRevitConnection} from "../utils/ConnectionManager.js";

export function registerSonneParameterProcedures(server: McpServer) {
  const guard = {_sonneModelId:z.string().min(1), _sonneDeadline:z.number(), _sonneOperationId:z.string().min(1)};
  const call = async (name:string, args:object) => {
    try {
      const result=await withRevitConnection(revit=>revit.sendCommand(name,args));
      return {content:[{type:"text" as const,text:JSON.stringify(result)}]};
    } catch(error) {return {isError:true,content:[{type:"text" as const,text:error instanceof Error?error.message:String(error)}]};}
  };
  server.tool("sonne_audit_parameters","Deterministic complete parameter audit. Returns compact counts and a single-use audit_id bound to exact editable matches. No C# generation or manual element ID copying needed.",{
    ...guard, parameter:z.string().min(1), scope:z.enum(["selection","category","model","elements"]).default("selection"),
    category:z.string().optional(),element_ids:z.array(z.string()).min(1).max(10000).optional(),
    rule:z.enum(["empty","equals","not_equals","all"]).default("empty"),expected:z.union([z.string(),z.number()]).optional(),
  },args=>call("sonne_audit_parameters",args));
  server.tool("sonne_bulk_set_parameters","Set a new value on exact editable matches from a fresh audit. Single atomic transaction, prior-value checks and automatic readback. No retry/save/sync.",{
    ...guard,audit_id:z.string().min(1),value:z.union([z.string(),z.number()]),
  },args=>call("sonne_bulk_set_parameters",args));
}
