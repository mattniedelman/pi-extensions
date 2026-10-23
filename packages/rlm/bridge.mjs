const BLOCKED_TOOLS = new Set(["python", "codemode"]);
const RESERVED_METHODS = new Set([
  "call", "tools", "search", "describe",
  "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue",
  "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is",
  "lambda", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
]);
const MAX_RESULT_BYTES = 1024 * 1024;

export function normalizeToolName(name) {
  let value = name.replace(/[^A-Za-z0-9_]/g, "_");
  if (!/^[A-Za-z_]/.test(value)) value = `tool_${value}`;
  return value;
}

export function toolCatalog(pi, ctx) {
  const info = new Map(pi.getAllTools().map((tool) => [tool.name, tool]));
  const counts = new Map();
  for (const tool of ctx.tools) {
    const method = normalizeToolName(tool.name);
    counts.set(method, (counts.get(method) ?? 0) + 1);
  }
  return ctx.tools
    .filter((tool) => !BLOCKED_TOOLS.has(tool.name))
    .map((tool) => {
      const metadata = info.get(tool.name);
      const method = normalizeToolName(tool.name);
      return {
        name: tool.name,
        method: RESERVED_METHODS.has(method) || counts.get(method) !== 1 ? null : method,
        description: tool.description,
        parameters: tool.parameters,
        output_schema: tool.outputSchema,
        namespace: metadata?.namespace?.name,
        annotations: metadata?.annotations,
      };
    });
}

function conciseCatalog(catalog) {
  return catalog.map(({ name, method, description }) => ({ name, method, description }));
}

function searchCatalog(catalog, query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  return catalog
    .map((tool) => ({
      tool,
      score: words.reduce((score, word) => score + (`${tool.name} ${tool.description}`.toLowerCase().includes(word) ? 1 : 0), 0),
    }))
    .filter(({ score }) => score > 0 || words.length === 0)
    .sort((a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name))
    .slice(0, 20)
    .map(({ tool }) => ({ name: tool.name, method: tool.method, description: tool.description }));
}

export async function handleBridgeCall(pi, ctx, name, args, signal) {
  const catalog = toolCatalog(pi, ctx);
  if (name === "__bridge_tools__") {
    return { content: "", structured: conciseCatalog(catalog), is_error: false };
  }
  if (name === "__bridge_search__") {
    return { content: "", structured: searchCatalog(catalog, String(args?.query ?? "")), is_error: false };
  }
  if (name === "__bridge_describe__") {
    const tool = catalog.find((item) => item.name === args?.name);
    return tool
      ? { content: "", structured: tool, is_error: false }
      : { content: `Tool ${String(args?.name)} not found`, structured: null, is_error: true };
  }
  if (BLOCKED_TOOLS.has(name)) {
    return { content: `Calling ${name} from the python tool is not allowed`, structured: null, is_error: true };
  }
  if (!catalog.some((tool) => tool.name === name)) {
    return { content: `Tool ${name} is not callable in this session`, structured: null, is_error: true };
  }
  const outcome = await ctx.executeTool(name, args ?? {}, { signal });
  const unsupported = outcome.result.content.find((block) => block.type !== "text");
  if (unsupported) {
    return { content: `${unsupported.type} results are not supported by the python bridge`, structured: null, is_error: true };
  }
  const content = outcome.result.content
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("\n");
  const envelope = {
    content,
    structured: outcome.result.structuredContent ?? null,
    is_error: outcome.isError,
  };
  const bytes = Buffer.byteLength(JSON.stringify(envelope));
  if (bytes > MAX_RESULT_BYTES) {
    return {
      content: `Tool result is ${bytes} bytes, above the ${MAX_RESULT_BYTES} byte python bridge limit`,
      structured: null,
      is_error: true,
    };
  }
  return envelope;
}
