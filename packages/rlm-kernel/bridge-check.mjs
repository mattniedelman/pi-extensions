import assert from "node:assert/strict";
import { handleBridgeCall, normalizeToolName, toolCatalog } from "./bridge.mjs";

const calls = [];
const tools = [
  { name: "read", description: "Read a file", parameters: { type: "object" }, outputSchema: { type: "object" } },
  { name: "foo-bar", description: "First collision", parameters: {} },
  { name: "foo_bar", description: "Second collision", parameters: {} },
  { name: "python", description: "Recursive", parameters: {} },
  { name: "codemode", description: "Recursive", parameters: {} },
  { name: "class", description: "Python keyword", parameters: {} },
];
const ctx = {
  tools,
  async executeTool(name, args, options) {
    calls.push({ name, args, signal: options.signal });
    if (name === "foo-bar") return { isError: true, result: { content: [{ type: "text", text: "blocked" }], details: {} } };
    return { isError: false, result: { content: [{ type: "text", text: `text:${name}` }], details: {}, structuredContent: { name, args } } };
  },
};
const pi = { getAllTools: () => tools.map((tool) => ({ ...tool, exposure: "direct" })) };

assert.equal(normalizeToolName("mcp__x-y"), "mcp__x_y");
const catalog = toolCatalog(pi, ctx);
assert.equal(catalog.find((t) => t.name === "read").method, "read");
assert.equal(catalog.find((t) => t.name === "foo-bar").method, null);
assert.equal(catalog.find((t) => t.name === "foo_bar").method, null);
assert.equal(catalog.some((t) => t.name === "python"), false);
assert.equal(catalog.find((t) => t.name === "class").method, null);

const signal = new AbortController().signal;
const read = await handleBridgeCall(pi, ctx, "read", { path: "a" }, signal);
assert.deepEqual(read, { content: "text:read", structured: { name: "read", args: { path: "a" } }, is_error: false });
assert.equal(calls[0].signal, signal);
const blocked = await handleBridgeCall(pi, ctx, "foo-bar", {}, signal);
assert.equal(blocked.is_error, true);
assert.equal(blocked.content, "blocked");
assert.equal((await handleBridgeCall(pi, ctx, "python", {}, signal)).is_error, true);
assert.equal((await handleBridgeCall(pi, ctx, "codemode", {}, signal)).is_error, true);
const search = await handleBridgeCall(pi, ctx, "__bridge_search__", { query: "file" }, signal);
assert.equal(search.structured[0].name, "read");
const described = await handleBridgeCall(pi, ctx, "__bridge_describe__", { name: "read" }, signal);
assert.equal(described.structured.name, "read");

const imageCtx = { ...ctx, executeTool: async () => ({ isError: false, result: { content: [{ type: "image", data: "x", mimeType: "image/png" }], details: {} } }) };
assert.equal((await handleBridgeCall(pi, imageCtx, "read", {}, signal)).is_error, true);
const hugeCtx = { ...ctx, executeTool: async () => ({ isError: false, result: { content: [{ type: "text", text: "x".repeat(1024 * 1024 + 1) }], details: {} } }) };
assert.equal((await handleBridgeCall(pi, hugeCtx, "read", {}, signal)).is_error, true);

const manyTools = Array.from({ length: 25 }, (_, i) => ({ name: `tool_${i}`, description: `Tool ${i}`, parameters: {} }));
const manyCtx = { ...ctx, tools: manyTools };
const manyPi = { getAllTools: () => manyTools };
const listed = await handleBridgeCall(manyPi, manyCtx, "__bridge_tools__", {}, signal);
assert.equal(listed.structured.length, 25);
console.log("rlm-kernel bridge checks passed");
