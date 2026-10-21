import assert from "node:assert/strict";
import { SidecarClient } from "./client.mjs";

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const rejects = (...args) => {
  const check = assert.rejects(...args);
  check.catch(() => undefined); // A deadline can fail before backup cleanup settles it.
  return check;
};
async function bounded(promise, label, ms = 700) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`${label} exceeded ${ms}ms`)), ms); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
const fake = `
  const mode = process.argv[1];
  if (mode.includes('stubborn')) process.on('SIGTERM', () => {});
  if (mode === 'early') { console.error('fixture bootstrap failed'); process.exit(7); }
  else if (mode === 'invalid') { console.log('{broken'); setInterval(() => {}, 1000); }
  else if (mode.startsWith('never')) setInterval(() => {}, 1000);
  else {
    console.log(JSON.stringify({ready: true}));
    require('node:readline').createInterface({input: process.stdin}).on('line', line => {
      const msg = JSON.parse(line);
      if (msg.operation === 'shutdown') {
        if (!mode.includes('stubborn')) process.exit(0);
      } else if (msg.code === 'nested') {
        console.log(JSON.stringify({id: msg.id, type: 'tool_call', name: 'hold', args: {}, call_id: 1}));
      } else if (msg.type !== 'tool_result' && mode !== 'ready-stubborn') {
        console.log(JSON.stringify({id: msg.id, ok: true, result: {pid: process.pid}}));
      }
    });
  }
`;

export async function lifecycleChecks(cwd) {
  const cases = [
    ["close during startup", async (make, track) => {
      const client = make("never");
      const child = track(client.child);
      const request = rejects(client.request("execute", "1"), /shutting down|exited/);
      await delay(20);
      await bounded(client.close(), "startup close");
      await bounded(request, "startup request rejection");
      await bounded(child.exit, "startup child exit");
      await rejects(client.request("execute", "2"), /shutting down/);
    }],
    ["abort during startup", async (make, track) => {
      const client = make("never");
      const child = track(client.child);
      const controller = new AbortController();
      const request = rejects(client.request("execute", "1", controller.signal), /aborted/);
      await delay(20);
      controller.abort();
      await bounded(request, "startup abort");
      await bounded(child.exit, "aborted startup child exit");
      await bounded(client.close(), "aborted startup close");
    }],
    ["startup timeout kills stubborn child", async (make, track) => {
      const client = make("never-stubborn", { startupTimeoutMs: 100 });
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "1"), /startup timed out after 100ms/), "startup timeout");
      await bounded(child.exit, "startup timeout child exit", 3000);
      await bounded(client.close(), "timed out startup close");
    }],
    ["invalid JSON", async (make, track) => {
      const client = make("invalid");
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "1"), /invalid JSON: \{broken/), "invalid JSON rejection");
      await bounded(client.close(), "invalid JSON close");
      await bounded(child.exit, "invalid JSON child exit");
    }],
    ["early exit diagnostic", async (make, track) => {
      const client = make("early");
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "1"), /exited \(7\): fixture bootstrap failed/), "early exit rejection");
      await bounded(client.close(), "early exit close");
      await bounded(child.exit, "early child exit");
    }],
    ["missing uv", async (make, track) => {
      const client = make("", { command: `${cwd}/missing-uv`, args: [] });
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "1"), /ENOENT/), "missing uv rejection");
      await bounded(client.close(), "missing uv close");
      await bounded(child.exit, "failed spawn close");
    }],
    ["request timeout kills stubborn child", async (make, track) => {
      const client = make("ready-stubborn", { requestTimeoutMs: 100 });
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "1"), /request timed out after 100ms/), "request timeout");
      await bounded(child.exit, "request timeout child exit", 3000);
      await bounded(client.close(), "timed out request close");
    }],
    ["concurrent close waits for stubborn child", async (make, track) => {
      const client = make("ready-stubborn");
      const child = track(client.child);
      await bounded(client.ready, "fixture ready");
      await bounded(Promise.all([client.close(), client.close()]), "concurrent close", 3000);
      assert.equal(child.exited, true, "close returned with its child still alive");
    }],
    ["active nested abort and lazy restart", async (make, track) => {
      const client = make("ready", { requestTimeoutMs: 40 });
      const old = track(client.child);
      const controller = new AbortController();
      let enter, finish;
      const entered = new Promise((resolve) => { enter = resolve; });
      const tool = new Promise((resolve) => { finish = resolve; });
      let settled = false;
      const active = client.request("execute", "nested", controller.signal, () => { enter(); return tool; });
      const rejected = rejects(active, /aborted/);
      active.then(() => { settled = true; }, () => { settled = true; });
      await bounded(entered, "nested call entry");
      await delay(100);
      assert.equal(settled, false, "response timer must pause inside a pi tool");
      controller.abort();
      await bounded(rejected, "nested abort");
      await bounded(old.exit, "nested abort child exit");
      const restarted = client.request("variables");
      // run() advances from the sequential queue in a microtask.
      await delay(0);
      const fresh = track(client.child);
      assert.notEqual(fresh.child.pid, old.child.pid);
      assert.equal((await bounded(restarted, "lazy restart")).pid, fresh.child.pid);
      finish({ content: "late", structured: null, is_error: false });
      await delay(20);
      assert.equal((await bounded(client.request("variables"), "after late tool result")).pid, fresh.child.pid);
      await bounded(client.close(), "restarted close");
      await bounded(fresh.exit, "restarted child exit");
    }],
    ["active nested close rejects queued shutdown work", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      let enter, finish;
      const entered = new Promise((resolve) => { enter = resolve; });
      const tool = new Promise((resolve) => { finish = resolve; });
      const active = rejects(client.request("execute", "nested", undefined, () => { enter(); return tool; }), /shutting down|exited/);
      await bounded(entered, "active call entry");
      const queued = rejects(client.request("variables"), /shutting down/);
      await bounded(client.close(), "active close");
      await bounded(Promise.all([active, queued]), "active and queued rejection");
      await bounded(child.exit, "active close child exit");
      finish({ content: "late", structured: null, is_error: false });
      await delay(20);
      await bounded(client.close(), "idempotent close");
    }],
  ];
  const failures = [];
  let observed = 0;
  for (const [name, check] of cases) {
    const owned = new Map();
    const clients = [];
    const track = (child) => {
      if (!owned.has(child)) {
        const record = { child, exited: false };
        record.exit = new Promise((resolve) => child.once("close", () => { record.exited = true; resolve(); }));
        owned.set(child, record);
      }
      return owned.get(child);
    };
    const make = (mode, options = {}) => {
      const client = new SidecarClient({ command: process.execPath, args: ["-e", fake, mode], cwd, ...options });
      clients.push(client);
      track(client.child);
      return client;
    };
    try {
      await check(make, track);
      for (const record of owned.values()) {
        assert.equal(record.exited, true, `${name}: owned child survived BEFORE backup cleanup`);
        if (record.child.pid) assert.throws(() => process.kill(record.child.pid, 0), { code: "ESRCH" });
      }
      console.log(`PASS lifecycle: ${name}`);
    } catch (error) {
      failures.push(`${name}: ${error.message}`);
      console.error(`FAIL lifecycle: ${name}: ${error.message}`);
    } finally {
      // Backup cleanup uses only handles returned by our own spawns, never a scan.
      for (const client of clients) {
        clearTimeout(client.readyTimer);
        client.stopping = true;
        if (!owned.has(client.child)) track(client.child);
      }
      for (const record of owned.values()) {
        if (!record.exited) record.child.kill("SIGKILL");
        await bounded(record.exit, `${name}: backup child exit`);
      }
      observed += owned.size;
    }
  }
  assert.deepEqual(failures, [], "bounded lifecycle counterexamples");
  console.log(`rlm-kernel lifecycle checks passed: ${cases.length}/${cases.length}; ${observed} owned children, 0 survivors BEFORE backup`);
}
