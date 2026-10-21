// Injected wiring checks, not evidence of real pi permission/validation hooks.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import factory from "./index.ts";

export default function (host) {
  host.registerCommand("probe-rlm", {
    description: "Run the actual RLM factory with a controlled context, without a model",
    handler: async () => {
      const events = new Map();
      let tool;
      factory({
        on(name, handler) { events.set(name, handler); },
        registerTool(value) { tool = value; },
        getAllTools() { return []; },
      });
      const ctx = {
        cwd: process.cwd(),
        sessionManager: {
          getSessionFile: () => `${process.env.PI_CODING_AGENT_SESSION_DIR}/injected.jsonl`,
          getSessionId: () => "injected",
        },
        tools: [],
        executeTool: async () => { throw new Error("unexpected nested call"); },
      };
      const failures = [];
      try {
        await events.get("session_start")({}, ctx);
        for (const [label, params, context, expected] of [
          ["non-string code", { code: 42 }, ctx, /code.*string/i],
          ["missing callable tools", { code: "42" }, { ...ctx, tools: undefined }, /callable.*tools.*context/i],
          ["non-callable executeTool", { code: "42" }, { ...ctx, executeTool: true }, /Nested pi tools.*context/],
        ]) {
          try { await assert.rejects(tool.execute("test", params, undefined, undefined, context), expected); }
          catch (error) { failures.push(`${label}: ${error.message}`); }
        }
        assert.deepEqual(failures, []);
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify({ passed: 3 }));
      } catch (error) {
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify({ error: error.stack }));
      } finally {
        await events.get("session_shutdown")();
      }
    },
  });
}
