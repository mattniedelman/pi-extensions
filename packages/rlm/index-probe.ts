// Injected wiring checks, not evidence of real pi permission/validation hooks.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import factory from "./index.ts";
import { renderingChecks } from "./render-probe.ts";

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
  } catch (error) {
    if (error.code !== "ENOENT" && error.code !== "ESRCH") throw error;
  }
  return [parent, ...[...children].flatMap((child) => tree(child))];
}

async function waitFor(predicate, timeout = 10000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = predicate();
    if (value) return value;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error("probe wait timed out");
}

function sessionManager() {
  return {
    branch: [],
    getSessionFile() { return undefined; },
    getSessionId() { return "injected"; },
    getBranch() { return this.branch; },
  };
}

export default function (host) {
  host.registerCommand("probe-rlm", {
    description: "Run the actual RLM factory with a controlled context, without a model",
    handler: async () => {
      const output = process.env.PI_RLM_PROBE_OUTPUT;
      const events = new Map();
      let tool;
      const injected = {
        on(name, handler) { assert.ok(!events.has(name)); events.set(name, handler); },
        registerTool(value) { assert.equal(tool, undefined); tool = value; },
        getAllTools() { return [tool, { name: "read", namespace: { name: "files" } }]; },
      };
      factory(injected);
      let passed = 0;
      const check = async (action) => { await action(); passed++; };
      const sm = sessionManager();
      const callable = { name: "read", description: "fixture", parameters: { type: "object" } };
      const ctx = {
        cwd: process.cwd(), sessionManager: sm, tools: [callable],
        async executeTool(name, args) { return { result: { content: [{ type: "text", text: "fixture" }], structuredContent: { name, args } }, isError: false }; },
      };
      const invoke = (params, context = ctx, signal) => tool.execute("probe", params, signal, undefined, context);
      const details = async (params, context = ctx, signal) => (await invoke(params, context, signal)).details;
      const execute = (code, context = ctx, signal) => details({ code }, context, signal);
      const start = (event = {}, context = ctx) => events.get("session_start")(event, context);
      const stop = () => events.get("session_shutdown")();
      const expectNotice = async () => {
        await assert.rejects(execute("1"), new RegExp(FRESH.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
        assert.equal((await execute("1")).result, 1);
      };
      try {
        await check(async () => {
          assert.equal(tool.name, "python");
          assert.equal(tool.exposure, "model-only");
          assert.deepEqual([...events.keys()], ["session_start", "session_shutdown"]);
          assert.deepEqual(tool.parameters.properties.operation.anyOf.map((schema) => schema.const), ["execute", "variables", "reset", "checkpoint"]);
        });
        await check(async () => {
          await assert.rejects(execute("1"), /kernel has not started/);
          assert.deepEqual(tree().slice(1), []);
        });
        await start({ reason: "startup" });
        await check(async () => assert.equal((await execute("x = 41\nx + 1")).result, 42));
        await check(async () => assert.equal((await execute("x += 1\nx")).result, 42));
        await check(async () => {
          const result = await details({ operation: "variables" }, null);
          assert.ok(result.names.includes("x"));
          assert.equal(result.truncated, false);
        });
        await check(async () => assert.deepEqual(await details({ operation: "reset" }, null), { reset: true, state_lost: true, notice: RESET }));
        await check(async () => await assert.rejects(execute("x"), /NameError/));
        await check(async () => await assert.rejects(invoke({ operation: "checkpoint" }, null), new RegExp(CHECKPOINT.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))));
        await check(async () => await assert.rejects(invoke({}), /code is required/));
        await check(async () => await assert.rejects(invoke({ code: "", operation: "execute" }), /code is required/));
        await check(async () => await assert.rejects(invoke({ code: "1" }, null), /Nested pi tools/));
        await check(async () => {
          const result = await execute("pi.read(path='a')['structured']['args']['path']");
          assert.equal(result.result, "a");
        });
        await check(async () => {
          sm.branch = [{ type: "model_change" }, { type: "usage" }];
          await start({ reason: "startup" });
          await expectNotice(); // replacing a live client warns even when entries are metadata-only
        });
        for (const reason of ["reload", "resume", "fork"]) {
          await check(async () => { await start({ reason }); await expectNotice(); });
        }
        await check(async () => { await start({ reason: "new", previousSessionFile: "old.jsonl" }); await expectNotice(); });
        await check(async () => {
          await stop();
          sm.branch = [{ type: "branch_summary" }];
          await start({ reason: "startup" });
          await expectNotice();
        });
        await check(async () => {
          await stop();
          sm.branch = [{ type: "model_change" }, { type: "thinking_level_change" }, { type: "usage" }, { type: "label" }, { type: "session_info" }];
          await start({ reason: "startup" });
          assert.equal((await execute("2")).result, 2);
        });
        await check(async () => {
          const descendants = await waitFor(() => tree().slice(1).length >= 3 && tree().slice(1));
          const sidecar = descendants.find(({ pid }) => readFileSync(`/proc/${pid}/cmdline`).includes("sidecar.py"));
          assert.ok(sidecar);
          const argv = readFileSync(`/proc/${sidecar.pid}/cmdline`).toString().split("\0").filter(Boolean);
          assert.ok(argv.some((value) => value.endsWith("sidecar.py")));
          assert.ok(!argv.includes("--state"));
          assert.ok(!argv.some((value) => /kernel\.bin|hash/.test(value)));
        });
        await check(async () => {
          const descendants = tree().slice(1);
          const sidecar = descendants.find(({ pid }) => readFileSync(`/proc/${pid}/cmdline`).includes("sidecar.py"));
          const pairs = readFileSync(`/proc/${sidecar.pid}/environ`).toString().split("\0").filter(Boolean);
          const env = Object.fromEntries(pairs.map((pair) => [pair.slice(0, pair.indexOf("=")), pair.slice(pair.indexOf("=") + 1)]));
          const allowed = new Set(["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TEMP", "TMP", "HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "JUPYTER_CONFIG_DIR", "JUPYTER_DATA_DIR", "JUPYTER_RUNTIME_DIR", "IPYTHONDIR", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PROJECT_ENVIRONMENT"]);
          assert.deepEqual(Object.keys(env).filter((key) => !allowed.has(key)), []);
          assert.equal(env.UV_OFFLINE, "1");
          assert.equal(env.UV_PROJECT_ENVIRONMENT, process.env.PI_RLM_ENV_DIR);
          assert.equal(env.PYTHONPATH, undefined);
          assert.equal(env.NODE_OPTIONS, undefined);
        });
        await check(async () => {
          const sidecar = tree().slice(1).find(({ pid }) => readFileSync(`/proc/${pid}/cmdline`).includes("sidecar.py"));
          process.kill(sidecar.pid, "SIGKILL");
          await waitFor(() => identity(sidecar.pid) === null);
          await expectNotice();
        });
        await check(async () => {
          await start({ reason: "new" });
          await expectNotice(); // live replacement always wins over an otherwise-empty new event
        });
        await check(async () => {
          await stop(); await stop();
          await assert.rejects(execute("1"), /kernel has not started/);
        });
        assert.equal(passed, 24);
        writeFileSync(output, JSON.stringify({ passed, rendering: renderingChecks().passed, wiringOnly: true, cleanup: "0 survivors" }));
      } catch (error) {
        writeFileSync(output, JSON.stringify({ error: error.stack, passed }));
      } finally {
        await stop();
      }
    },
  });
}
