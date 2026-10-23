// No-model readiness, not evidence of real permission/validation hooks.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
// The runner writes a tiny loader importing this probe and the independent factory.

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
    const argv = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0").filter(Boolean);
    assert.equal(identity(pid)?.start, parent.start, "owned parent identity changed");
    return [{ ...parent, argv }, ...[...children].flatMap((child) => tree(child))];
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return [];
    throw error;
  }
}

function kernelTree(sessionIdentity) {
  const state = join(process.env.PI_RLM_STATE_DIR, createHash("sha256").update(sessionIdentity).digest("hex"), "kernel.bin");
  const owned = tree().slice(1);
  const members = new Set(owned.filter((child) => child.argv.includes(state)).map((child) => child.pid));
  for (const child of owned) if (members.has(child.parent)) members.add(child.pid);
  return owned.filter((child) => members.has(child.pid));
}

function retired(generation) {
  for (const old of generation) assert.notEqual(identity(old.pid)?.start, old.start, "owned generation survived BEFORE backup");
}

export default function probe(host, factory, source) {
  host.registerCommand("probe-rlm", {
    description: "Prove independent package discovery and real sidecar readiness without a model",
    handler: async (_args, hostContext) => {
      const sourceStat = statSync(source);
      const result = { source: realpathSync(source), sourceIdentity: { device: sourceStat.dev, inode: sourceStat.ino }, generations: [] };
      const events = new Map();
      let tool;
      let phase = "discovery";
      const file = join(process.env.PI_CODING_AGENT_SESSION_DIR, "installed-oracle.jsonl");
      const context = {
        cwd: process.cwd(), tools: [],
        sessionManager: { getSessionFile: () => file, getSessionId: () => "installed-oracle" },
        executeTool() { throw new Error("no nested tool should run in readiness oracle"); },
      };
      const invoke = async (params, ctx = null) => {
        const reply = await tool.execute("installed-oracle", params, undefined, undefined, ctx);
        assert.deepEqual(JSON.parse(reply.content[0].text), reply.details);
        return reply.details;
      };
      const generation = () => {
        const children = kernelTree(file);
        assert.ok(children.length >= 3, "must observe real installed uv/Python/Monty generation");
        result.generations.push(children);
        return children;
      };
      try {
        // No factory injection has happened: this registration must come from package discovery.
        const registered = host.getAllTools().filter((entry) => entry.name === "python");
        assert.equal(registered.length, 1, "independent package was not automatically discovered");
        assert.equal(registered[0].exposure, "model-only");
        result.discovered = { name: registered[0].name, exposure: registered[0].exposure };
        result.automatic = tree().slice(1);
        const autoIdentity = hostContext.sessionManager.getSessionFile() ?? `${hostContext.cwd}\0${hostContext.sessionManager.getSessionId()}`;
        assert.notEqual(file, autoIdentity, "injected factory must not share the automatically loaded checkpoint");
        result.identities = { automatic: autoIdentity, injected: file };
        factory({
          on(name, handler) { events.set(name, handler); },
          registerTool(value) { tool = value; },
          getAllTools() { return host.getAllTools(); },
        });
        phase = "session_start";
        await events.get("session_start")({}, context);
        phase = "checkpoint";
        assert.equal((await invoke({ operation: "checkpoint" })).checkpointed, true);
        const deadline = performance.now() + 5000;
        while (kernelTree(autoIdentity).length < 3 && performance.now() < deadline) {
          await new Promise((resolve) => setTimeout(resolve, 10));
        }
        result.automatic = kernelTree(autoIdentity);
        assert.ok(result.automatic.length >= 3, "auto-loaded installed sidecar must run");
        assert.deepEqual((await invoke({ operation: "variables" })).names, []);
        result.values = [(await invoke({ code: "x = 42\nx" }, context)).result,
                         (await invoke({ operation: "execute", code: "x += 1\nx" }, context)).result];
        assert.deepEqual(result.values, [42, 43]);
        assert.deepEqual((await invoke({ operation: "variables" })).names, ["x"]);
        assert.equal((await invoke({ operation: "checkpoint" })).checkpointed, true);
        const first = generation();
        await events.get("session_shutdown")();
        retired(first);
        await events.get("session_start")({}, context);
        assert.equal((await invoke({ code: "x" }, context)).result, 43);
        const second = generation();
        assert.ok(second.every((child) => !first.some((old) => old.pid === child.pid && old.start === child.start)), "restored generation must be new");
        result.ready = true;
      } catch (error) {
        result.error = String(error?.stack ?? error);
        result.phase = phase;
        result.ready = false;
      } finally {
        try {
          await events.get("session_shutdown")?.();
          await events.get("session_shutdown")?.();
          for (const old of result.generations) retired(old);
          result.closed = true;
        } catch (error) {
          result.error = String(error?.stack ?? error);
          result.ready = false;
          result.closed = false;
        }
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify(result));
      }
    },
  });
}
