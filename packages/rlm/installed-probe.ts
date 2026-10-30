// Installed black-box oracle. This probe never imports repository runtime code.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { createInstalledGeneration } from "./installed-generation.mjs";

const FRESH = "Python workspace started fresh; state from an earlier process is not restored";
const RESET = "Python workspace reset; previous live state was discarded";
const CHECKPOINT = "Python checkpoints are unsupported; state lasts only for the live pi session.";

function identity(pid) {
  try {
    const fields = readFileSync(`/proc/${pid}/stat`, "utf8").split(")").at(-1).trim().split(/\s+/);
    return { pid, start: fields[19], parent: Number(fields[1]) };
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return null;
    throw error;
  }
}

function tree(pid = process.pid) {
  const parent = identity(pid);
  if (!parent) return [];
  const children = new Set();
  try {
    for (const task of readdirSync(`/proc/${pid}/task`)) {
      for (const child of readFileSync(`/proc/${pid}/task/${task}/children`, "utf8").trim().split(/\s+/)) if (child) children.add(Number(child));
    }
    const argv = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0").filter(Boolean);
    assert.equal(identity(pid)?.start, parent.start, "observed process identity changed");
    return [{ ...parent, argv }, ...[...children].flatMap((child) => tree(child))];
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return [];
    throw error;
  }
}

async function waitFor(predicate, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("installed probe wait timed out");
}

function callerError(error) {
  const value = error instanceof Error ? error : new Error(String(error));
  const fields = {};
  for (const key of Object.getOwnPropertyNames(value)) {
    if (["stack", "name", "message", "cause", "code"].includes(key)) continue;
    const field = value[key];
    if (field === null || ["string", "number", "boolean"].includes(typeof field)) fields[key] = field;
  }
  return {
    name: value.name,
    code: typeof value.code === "string" ? value.code : null,
    message: value.message,
    cause: value.cause === undefined ? null : callerError(value.cause),
    fields,
  };
}


function provider(host) {
  let code;
  let continuation = false;
  let request = 0;
  host.on("before_agent_start", (event) => {
    const first = event.prompt.split("\n")[0];
    const legacy = {
      "LEGACY startup-notice": { code: "1" },
      "LEGACY success": { code: "legacy_value = 42\nlegacy_value" },
      "LEGACY failed-mutation": { code: "legacy_value = 43\nraise RuntimeError('legacy mutation failed')" },
      "LEGACY variables": { operation: "variables" },
      "LEGACY reset": { operation: "reset" },
      "LEGACY post-reset": { code: "legacy_value" },
      "LEGACY reload-notice": { code: "1" },
      "LEGACY post-reload": { code: "legacy_value" },
    };
    code = legacy[first] ?? (first === "PROCESS_AUTO" ? { code: "1" } : first === "PROCESS_A" ? { code: "process_value = 99\nprocess_value" } : first === "PROCESS_B_NOTICE" ? { code: "1" } : first === "PROCESS_B_NAME" ? { code: "process_value" } : undefined);
    continuation = false;
  });
  host.registerProvider("rlm-installed-local", {
    api: "rlm-installed-local-api", baseUrl: "local://no-network", apiKey: "local-test-only-not-a-credential",
    models: [{ id: "fixed", name: "Installed deterministic transport", reasoning: false, input: ["text"], contextWindow: 8192, maxTokens: 256,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, _context, options) {
      const stream = createAssistantMessageEventStream();
      const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id, content: [], timestamp: Date.now(), stopReason: "pending",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      (async () => {
        try {
          if (!code || ++request > 16) throw new Error("unexpected installed provider request");
          const emit = async (event) => { stream.push(event); };
          await emit({ type: "start", partial: message });
          if (!continuation) {
            const call = { type: "toolCall", id: "call_" + String(request).padStart(32, "0"), name: "python", arguments: code };
            message.content.push(call);
            await emit({ type: "toolcall_start", contentIndex: 0, partial: message });
            await emit({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message });
            message.stopReason = "toolUse";
            continuation = true;
          } else {
            message.content.push({ type: "text", text: "DONE" });
            await emit({ type: "text_start", contentIndex: 0, partial: message });
            await emit({ type: "text_end", contentIndex: 0, content: "DONE", partial: message });
            message.stopReason = "stop";
          }
          await emit({ type: "done", reason: message.stopReason, message });
        } catch {
          message.stopReason = "error";
          stream.push({ type: "error", reason: "error", error: message });
        } finally { stream.end(); }
      })();
      return stream;
    },
  });
}

export default function probe(host, factory, source) {
  provider(host);
  const mode = process.env.PI_RLM_PROBE_MODE;
  if (mode === "provider") return;
  if (mode === "legacy-state") {
    const log = (value) => writeFileSync(process.env.PI_RLM_LEGACY_LOG, JSON.stringify(value) + "\n", { flag: "a" });
    host.on("session_start", (event, ctx) => log({ type: "start", reason: event.reason, sessionFile: ctx.sessionManager.getSessionFile(), tools: host.getAllTools().filter((tool) => tool.name === "python").length }));
    host.on("tool_call", (event) => { if (event.toolName === "python") log({ type: "call", arguments: event.input }); });
    host.on("tool_result", (event) => { if (event.toolName === "python") log({ type: "result", isError: event.isError }); });
    host.registerCommand("legacy-reload", { description: "Reload legacy-state lifecycle fixture", handler: async (_args, ctx) => { await ctx.reload(); } });
    return;
  }
  const sourceStat = statSync(source);
  const base = { source: realpathSync(source), sourceIdentity: { device: sourceStat.dev, inode: sourceStat.ino } };
  const sidecar = base.source.replace(/index\.ts$/, "python/sidecar.py");
  const installedGeneration = createInstalledGeneration(sidecar, () => tree().slice(1));
  host.registerCommand("probe-rlm", {
    description: "Prove installed discovery or injected session-only semantics",
    handler: async (_args, hostContext) => {
      const result = { ...base, mode };
      let shutdown;
      try {
        if (mode === "automatic") {
          const registered = host.getAllTools().filter((entry) => entry.name === "python");
          assert.equal(registered.length, 1, "installed python tool was not automatically discovered");
          assert.equal(registered[0].exposure, "model-only");
          result.discovered = { name: registered[0].name, exposure: registered[0].exposure };
          result.generation = await waitFor(() => installedGeneration());
        } else {
          assert.equal(mode, "injected");
          assert.equal(host.getAllTools().filter((entry) => entry.name === "python").length, 0, "automatic discovery must be disabled");
          const before = new Set(tree().slice(1).map((child) => `${child.pid}:${child.start}`));
          const events = new Map();
          let tool;
          factory({
            on(name, handler) { events.set(name, handler); },
            registerTool(value) { tool = value; },
            getAllTools() { return [tool]; },
          });
          const context = { cwd: process.cwd(), tools: [{ name: "read", description: "fixture", parameters: { type: "object" } }], sessionManager: { getBranch: () => [] }, async executeTool(name, args) { return { result: { content: [{ type: "text", text: "fixture" }], structuredContent: { name, args } }, isError: false }; } };
          const invoke = async (params, ctx = null) => {
            const reply = await tool.execute("installed", params, undefined, undefined, ctx);
            assert.deepEqual(JSON.parse(reply.content[0].text), reply.details);
            return reply.details;
          };
          shutdown = events.get("session_shutdown");
          await events.get("session_start")({ reason: "startup" }, context);
          if (process.env.PI_RLM_BOOTSTRAP_EXPECTED) {
            let bootstrapError;
            try {
              await invoke({ code: "1" }, context);
            } catch (error) {
              bootstrapError = callerError(error);
            }
            assert.ok(bootstrapError, "bootstrap fixture unexpectedly succeeded");
            result.bootstrap = process.env.PI_RLM_BOOTSTRAP_EXPECTED;
            result.bootstrapError = bootstrapError;
            result.ready = true;
            return;
          }
          result.generation = await waitFor(() => {
            const generation = installedGeneration();
            return generation && generation.every((child) => !before.has(`${child.pid}:${child.start}`)) && generation;
          });
          assert.equal((await invoke({ code: "x = 42\nx" }, context)).result, 42);
          assert.equal((await invoke({ code: "x += 1\nx" }, context)).result, 43);
          await assert.rejects(invoke({ code: "x = 44\nraise RuntimeError('mutation failed')" }, context), /mutation failed/);
          assert.equal((await invoke({ code: "x" }, context)).result, 44);
          await invoke({ operation: "reset" }).then((value) => assert.deepEqual(value, { reset: true, state_lost: true, notice: RESET }));
          const names = Array.from({ length: 300 }, (_, index) => `n${String(index).padStart(3, "0")}`);
          await invoke({ code: `for name in list(globals()):\n    if not name.startswith('_') and name != 'pi': del globals()[name]\nglobals().update({name: i for i, name in enumerate(${JSON.stringify(names)})})\nlen([name for name in globals() if name.startswith('n')])` }, context);
          assert.deepEqual(await invoke({ operation: "variables" }), { names: names.slice(0, 256), truncated: true });
          await invoke({ code: "saved = pi.read" }, context);
          await assert.rejects(invoke({ code: "saved(path='expired')" }, context), /pi tool context expired/);
          await assert.rejects(invoke({ operation: "checkpoint" }), new RegExp(CHECKPOINT.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
          await events.get("session_start")({ reason: "reload" }, context);
          await assert.rejects(invoke({ code: "1" }, context), new RegExp(FRESH.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
          await assert.rejects(invoke({ code: "x" }, context), /NameError/);
          await shutdown();
          shutdown = undefined;
          result.values = [42, 43, 44];
          result.variables = 256;
          result.reset = RESET;
          result.reload = FRESH;
          result.checkpoint = CHECKPOINT;
        }
        result.ready = true;
      } catch (error) {
        result.ready = false;
        result.error = String(error?.stack ?? error);
      } finally {
        try { await shutdown?.(); } catch {}
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify(result));
      }
    },
  });
}
