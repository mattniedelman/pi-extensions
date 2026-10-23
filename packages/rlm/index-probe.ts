// Injected wiring checks, not evidence of real pi permission/validation hooks.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, statSync, writeFileSync, existsSync } from "node:fs";
import { join } from "node:path";
import factory from "./index.ts";
import { renderingChecks } from "./render-probe.ts";

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
      for (const child of readFileSync(`/proc/${pid}/task/${task}/children`, "utf8").trim().split(/\s+/)) {
        if (child) children.add(Number(child));
      }
    }
  } catch (error) {
    if (error.code !== "ENOENT" && error.code !== "ESRCH") throw error;
  }
  assert.equal(identity(pid)?.start, parent.start, "observed parent identity changed");
  return [parent, ...[...children].flatMap((child) => tree(child))];
}

function retired(identities) {
  for (const old of identities) assert.notEqual(identity(old.pid)?.start, old.start, "owned identity survived BEFORE backup");
}

function liveKernel() {
  const identities = tree().slice(1);
  assert.ok(identities.length >= 3, "must observe real uv/Python/Monty before lifecycle action");
  return identities;
}

function sessionManager(file, id = "injected") {
  return {
    file, id, leaf: "latest", branch: [{ id: "latest" }],
    getSessionFile() { return this.file; },
    getSessionId() { return this.id; },
    getLeafId() { return this.leaf; },
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
      const metadata = [{ name: "read", namespace: { name: "files" } }, { name: "stale" }];
      const injected = {
        on(name, handler) { assert.ok(!events.has(name)); events.set(name, handler); },
        registerTool(value) { assert.equal(tool, undefined); tool = value; },
        getAllTools() { return [tool, ...metadata]; },
      };
      factory(injected);
      let passed = 0;
      const check = async (action) => { await action(); passed++; };
      const calls = [];
      const callable = (name) => ({ name, description: `${name} fixture`, parameters: { type: "object" } });
      const sm = sessionManager(join(process.env.PI_CODING_AGENT_SESSION_DIR, "injected.jsonl"));
      const ctx = {
        cwd: process.cwd(), sessionManager: sm, tools: [callable("read")],
        async executeTool(name, args, options) {
          assert.equal(this, ctx);
          calls.push({ name, args, signal: options.signal });
          return { result: { content: [{ type: "text", text: "first" }], structuredContent: { value: 42 } }, isError: false };
        },
      };
      const invoke = (params, context = ctx, signal) => tool.execute("injected-parent", params, signal, undefined, context);
      const run = async (code, context = ctx, signal) => {
        const result = await invoke({ code }, context, signal);
        assert.deepEqual(JSON.parse(result.content[0].text), result.details);
        return result.details.result;
      };
      const start = (context = ctx) => events.get("session_start")({}, context);
      const stop = () => events.get("session_shutdown")();
      const state = (file) => join(process.env.PI_RLM_STATE_DIR, createHash("sha256").update(file).digest("hex"), "kernel.bin");
      try {
        await check(async () => {
          assert.equal(tool.name, "python");
          assert.equal(tool.exposure, "model-only");
          assert.deepEqual([...events.keys()], ["session_start", "session_shutdown"]);
          assert.deepEqual(Object.keys(tool.parameters.properties), ["operation", "code"]);
          assert.equal(tool.parameters.required, undefined);
          assert.deepEqual(tool.parameters.properties.operation.anyOf.map((schema) => schema.const), ["execute", "variables", "reset", "checkpoint"]);
          await assert.rejects(invoke({ code: "42" }), /kernel has not started/);
          assert.deepEqual(tree().slice(1), [], "factory must not launch a sidecar");
        });
        await start();
        for (const [params, context, expected] of [
          [{}, ctx, /code is required/],
          [{ code: "" }, ctx, /code is required/],
          [{ code: 42 }, ctx, /code.*string/i],
          [{ code: "42" }, undefined, /Nested pi tools.*context/],
          [{ code: "42" }, { ...ctx, executeTool: undefined }, /Nested pi tools.*context/],
          [{ code: "42" }, { ...ctx, executeTool: true }, /Nested pi tools.*context/],
          [{ code: "42" }, { ...ctx, tools: undefined }, /callable.*tools.*context/i],
          [{ code: "42" }, { ...ctx, tools: {} }, /callable.*tools.*context/i],
        ]) {
          await check(() => assert.rejects(tool.execute("negative", params, undefined, undefined, context), expected));
        }
        await check(async () => {
          assert.equal(await run("x = 42\nx"), 42);
          assert.equal((await invoke({ operation: "execute", code: "x += 1\nx" })).details.result, 43);
        });
        await check(async () => {
          // Non-execute operations do not require nested-tool context or code.
          assert.deepEqual((await invoke({ operation: "variables" }, null)).details.names, ["x"]);
          assert.equal((await invoke({ operation: "checkpoint" }, null)).details.checkpointed, true);
          await assert.rejects(invoke({ operation: "not-an-operation" }), /unknown operation/);
          assert.equal(statSync(state(sm.file)).mode & 0o777, 0o600);
          assert.equal(statSync(state(sm.file).replace(".bin", ".names.json")).mode & 0o777, 0o600);
          assert.equal(statSync(join(state(sm.file), "..")).mode & 0o777, 0o700);
        });
        const controller = new AbortController();
        await check(async () => {
          assert.equal(await run("saved = pi.read\npi.read(path='first.txt')['structured']['value']", ctx, controller.signal), 42);
          assert.deepEqual(calls, [{ name: "read", args: { path: "first.txt" }, signal: controller.signal }]);
        });
        const rebound = {
          ...ctx,
          async executeTool(name, args, options) {
            assert.equal(this, rebound);
            calls.push({ name, args, signal: options.signal });
            return { result: { content: [{ type: "text", text: "rebound" }], structuredContent: { value: 43 } }, isError: false };
          },
        };
        await check(async () => {
          assert.equal(await run("saved(path='next.txt')['structured']['value']", rebound, controller.signal), 43);
          assert.deepEqual(calls.at(-1), { name: "read", args: { path: "next.txt" }, signal: controller.signal });
          assert.equal(await run("pi.describe('read')['structured']['namespace']", rebound), "files");
        });
        await check(async () => {
          const empty = { ...rebound, tools: [] };
          const before = calls.length;
          assert.equal((await run("saved(path='removed.txt')", empty)).is_error, true);
          assert.equal((await run("pi.call('stale', {})", empty)).is_error, true);
          assert.deepEqual(await run("pi.tools()['structured']", empty), []);
          await assert.rejects(run("pi.read(path='removed.txt')", empty), /AttributeError/);
          assert.equal(calls.length, before, "removed/stale loadout must not dispatch");
          assert.equal(await run("pi.read(path='again.txt')['structured']['value']", rebound), 43);
        });
        await check(async () => {
          const old = liveKernel();
          await start(); // same file, replacement without a preceding shutdown
          retired(old);
          assert.equal(await run("x"), 43);
          assert.equal(await run("saved(path='restored.txt')['structured']['value']", rebound), 43);
        });
        await check(async () => {
          const old = liveKernel();
          await stop(); await stop();
          retired(old);
          const reloadedEvents = new Map();
          factory({ ...injected, on: (name, handler) => reloadedEvents.set(name, handler), registerTool: (value) => { tool = value; } });
          events.clear();
          for (const [name, handler] of reloadedEvents) events.set(name, handler);
          await start();
          assert.equal(await run("x"), 43);
        });
        await check(async () => {
          sm.leaf = "earlier"; sm.branch = [{ id: "earlier" }]; sm.id = "different-metadata";
          assert.equal(await run("x += 1\nx"), 44);
          await start();
          assert.equal(await run("x"), 44, "same file ignores branch and ID metadata");
        });
        const original = sm.file;
        for (const reason of ["new", "fork", "clone"]) {
          await check(async () => {
            sm.file = join(process.env.PI_CODING_AGENT_SESSION_DIR, `${reason}.jsonl`);
            sm.id = reason;
            const old = liveKernel();
            await start(); retired(old);
            await assert.rejects(run("x"), /NameError/);
            assert.equal(await run("fresh = 1\nfresh"), 1);
            assert.ok(existsSync(state(sm.file)));
          });
        }
        await check(async () => {
          sm.file = original;
          await start();
          assert.equal(await run("x"), 44);
          assert.deepEqual((await invoke({ operation: "reset" }, null)).details, { reset: true });
          assert.equal(existsSync(state(original)), false);
          assert.equal(existsSync(state(original).replace(".bin", ".names.json")), false);
          await assert.rejects(run("x"), /NameError/);
        });
        await check(async () => {
          sm.file = undefined; sm.id = "ephemeral-a";
          await start();
          assert.equal(await run("x = 42\nx"), 42);
          assert.ok(existsSync(state(`${ctx.cwd}\0ephemeral-a`)));
          await start(); assert.equal(await run("x"), 42);
          sm.id = "ephemeral-b"; await start(); await assert.rejects(run("x"), /NameError/);
          sm.id = "ephemeral-a"; await start(); assert.equal(await run("x"), 42);
          const otherCwd = join(process.env.PI_CODING_AGENT_DIR, "..");
          await start({ ...ctx, cwd: otherCwd }); await assert.rejects(run("x"), /NameError/);
          await start(); assert.equal(await run("x"), 42);
        });
        await check(async () => {
          const aborter = new AbortController();
          let started;
          const nested = new Promise((resolve) => { started = resolve; });
          const slow = {
            ...ctx, tools: [callable("slow")],
            async executeTool(name, args, options) {
              assert.equal(name, "slow"); assert.deepEqual(args, { value: 1 });
              assert.equal(options.signal, aborter.signal);
              started();
              await new Promise((resolve) => options.signal.addEventListener("abort", resolve, { once: true }));
              return { result: { content: [{ type: "text", text: "late" }] }, isError: false };
            },
          };
          const failed = assert.rejects(run("x = 99\npi.slow(value=1)", slow, aborter.signal), /aborted/);
          failed.catch(() => undefined);
          await nested;
          const old = liveKernel();
          aborter.abort(); await failed;
          assert.equal(await run("x"), 42, "aborted execution must restore last checkpoint");
          retired(old);
        });
        await check(async () => {
          const old = liveKernel();
          await stop(); await stop(); retired(old);
          await assert.rejects(invoke({ code: "42" }), /kernel has not started/);
        });
        writeFileSync(output, JSON.stringify({ passed, rendering: renderingChecks().passed, wiringOnly: true, cleanup: "old identities absent BEFORE backup" }));
      } catch (error) {
        writeFileSync(output, JSON.stringify({ error: error.stack, passed }));
      } finally {
        await stop();
      }
    },
  });
}
