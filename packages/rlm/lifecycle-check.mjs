import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { SidecarClient } from "./client.mjs";

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const rejects = (...args) => {
  const check = assert.rejects(...args);
  check.catch(() => undefined); // A deadline can fail before backup cleanup settles it.
  return check;
};
export async function bounded(promise, label, ms = 700) {
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
    let forcedRetired = false;
    const abortIds = [];
    require('node:readline').createInterface({input: process.stdin}).on('line', line => {
      const msg = JSON.parse(line);
      if (msg.operation === 'shutdown') {
        if (!mode.includes('stubborn')) process.exit(0);
      } else if (msg.operation === 'abort') {
        abortIds.push(msg.id);
        const forced = mode === 'forced-retirement';
        forcedRetired = forcedRetired || forced;
        console.log(JSON.stringify({id: msg.id, ok: true, result: {
          aborted: true,
          state_lost: forced,
          generation: forced ? 'replacement' : 'fixture',
          ...(forced ? {notice: 'Python workspace was lost after the interrupted kernel did not become idle'} : {}),
        }}));
      } else if (msg.code === 'nested') {
        console.log(JSON.stringify({id: msg.id, type: 'tool_call', name: 'hold', args: {}, call_id: 1}));
      } else if (msg.code === 'rejected-tool') {
        console.log(JSON.stringify({id: msg.id, type: 'tool_call', name: 'reject', args: {}, call_id: 1}));
      } else if (msg.code === 'unencodable-rejected-tool') {
        console.log(JSON.stringify({id: msg.id, type: 'tool_call', name: 'reject', args: {}, call_id: 1, generation: '\\\\'.repeat(524238)}));
      } else if (msg.code === 'exact-cap-no-context-tool' || msg.code === 'exact-cap-multibyte-no-context-tool') {
        const call = {id: msg.id, type: 'tool_call', name: 'orphan', args: {}, call_id: 1, generation: ''};
        const remaining = 1024 * 1024 - Buffer.byteLength(JSON.stringify(call));
        if (msg.code === 'exact-cap-no-context-tool') {
          call.generation = 'a'.repeat(remaining % 2) + '\\\\'.repeat(Math.floor(remaining / 2));
        } else {
          call.generation = 'a'.repeat(remaining % 4) + '\u{1f642}'.repeat(Math.floor(remaining / 4));
        }
        const frame = JSON.stringify(call);
        if (Buffer.byteLength(frame) !== 1024 * 1024) throw new Error('fixture frame is not exact-cap');
        console.log(frame);
      } else if (msg.type === 'tool_result') {
        console.log(JSON.stringify({id: msg.id, ok: true, result: {error: msg.error}}));
      } else if (mode !== 'ready-stubborn' && (mode !== 'forced-retirement' || forcedRetired)) {
        console.log(JSON.stringify({id: msg.id, ok: true, result: {pid: process.pid, requestId: msg.id, abortIds}}));
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
      const queued = rejects(client.request("variables"), /shutting down/);
      await bounded(client.close(), "startup close");
      await bounded(Promise.all([request, queued]), "startup and queued request rejection");
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
      assert.equal(child.child.signalCode, "SIGKILL", "stubborn startup must reach kill escalation");
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
      client.options.args = ["-e", fake, "ready"];
      const restarted = await bounded(client.request("variables"), "restart after timed out request", 3000);
      const fresh = track(client.child);
      assert.equal(child.exited, true, "restart must retire the prior child first");
      assert.equal(child.child.signalCode, "SIGKILL", "stubborn request must reach kill escalation");
      assert.equal(restarted.pid, fresh.child.pid);
      assert.notEqual(fresh.child.pid, child.child.pid);
      await bounded(client.close(), "timed out request close");
      await bounded(fresh.exit, "restarted timeout child exit");
    }],
    ["concurrent close waits for stubborn child", async (make, track) => {
      const client = make("ready-stubborn");
      const child = track(client.child);
      await bounded(client.ready, "fixture ready");
      await bounded(Promise.all([client.close(), client.close()]), "concurrent close", 3000);
      assert.equal(child.exited, true, "close returned with its child still alive");
      assert.equal(child.child.signalCode, "SIGKILL", "stubborn shutdown must reach kill escalation");
    }],
    ["active nested abort separates caller from retained barrier", async (make, track) => {
      const client = make("ready", { requestTimeoutMs: 40 });
      const child = track(client.child);
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
      assert.equal(child.exited, false, "successful interrupt must retain sidecar");
      const queued = client.request("variables");
      let queuedSettled = false;
      queued.finally(() => { queuedSettled = true; });
      await delay(20);
      assert.equal(queuedSettled, true, "matching aborted response must release barrier");
      assert.equal((await queued).pid, child.child.pid);
      finish({ content: "late", structured: null, is_error: false });
      await delay(20);
      assert.equal((await bounded(client.request("variables"), "after late tool result")).pid, child.child.pid);
      await bounded(client.close(), "retained close");
      await bounded(child.exit, "retained child exit");
    }],
    ["post-settlement abort is detached before fulfillment", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      const controller = new AbortController();
      const first = client.request("execute", "settles", controller.signal);
      first.then(() => controller.abort());
      assert.equal((await bounded(first, "settled request")).requestId, 1);
      const next = await bounded(client.request("variables"), "request after settled abort");
      assert.equal(next.requestId, 2, "settled abort must not consume a stale request ID");
      assert.deepEqual(next.abortIds, [], "settled abort must not reach the sidecar");
      assert.equal(next.pid, child.child.pid, "request after settled abort must retain the sidecar");
      await bounded(client.close(), "settled abort close");
      await bounded(child.exit, "settled abort child exit");
    }],
    ["queued abort rejects only its caller and never reaches sidecar", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      let enter, finish;
      const entered = new Promise((resolve) => { enter = resolve; });
      const tool = new Promise((resolve) => { finish = resolve; });
      const active = client.request("execute", "nested", undefined, () => { enter(); return tool; });
      await bounded(entered, "active call entry");
      const controller = new AbortController();
      const queued = rejects(client.request("execute", "must-not-run", controller.signal), /aborted/);
      controller.abort();
      await bounded(queued, "queued abort");
      assert.equal(child.exited, false, "queued abort must not stop the active sidecar");
      finish({ content: "done", structured: null, is_error: false });
      assert.equal((await bounded(active, "active request after queued abort")).error, undefined);
      assert.equal((await bounded(client.request("variables"), "request after queued abort")).pid, child.child.pid);
      await bounded(client.close(), "queued abort close");
      await bounded(child.exit, "queued abort child exit");
    }],
    ["forced retirement notice reaches the next caller once", async (make, track) => {
      const client = make("forced-retirement");
      const child = track(client.child);
      const controller = new AbortController();
      const active = rejects(client.request("execute", "1", controller.signal), /aborted/);
      await bounded(client.ready, "forced retirement fixture ready");
      controller.abort();
      await bounded(active, "forced retirement caller abort");
      const next = rejects(
        client.request("variables"),
        /Python workspace was lost after the interrupted kernel did not become idle/,
      );
      await bounded(next, "forced retirement notice delivery");
      const after = await bounded(client.request("variables"), "one-shot notice follow-up");
      assert.equal(after.pid, child.child.pid);
      await bounded(client.close(), "forced retirement close");
      await bounded(child.exit, "forced retirement child exit");
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
    ["escaped tool rejection at payload cap", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      const detail = "\\".repeat((256 * 1024 - 2) / 2);
      const result = await bounded(client.request("execute", "rejected-tool", undefined, () => Promise.reject(detail)), "accepted escaped rejection");
      assert.equal(result.error, detail);
      await bounded(client.close(), "accepted escaped rejection close");
      await bounded(child.exit, "accepted escaped rejection child exit");
    }],
    ["escaped tool rejection over payload cap", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      const detail = "\\".repeat(512 * 1024);
      const result = await bounded(client.request("execute", "rejected-tool", undefined, () => Promise.reject(detail)), "oversized escaped rejection");
      assert.match(result.error, /exceeds 262144 bytes/);
      await bounded(client.close(), "oversized escaped rejection close");
      await bounded(child.exit, "oversized escaped rejection child exit");
    }],
    ["multibyte tool rejection at payload cap", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      const detail = "\u{1f642}".repeat(65535);
      const result = await bounded(client.request("execute", "rejected-tool", undefined, () => Promise.reject(detail)), "accepted multibyte rejection");
      assert.equal(result.error, detail);
      await bounded(client.close(), "accepted multibyte rejection close");
      await bounded(child.exit, "accepted multibyte rejection child exit");
    }],
    ["multibyte tool rejection over payload cap", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      const detail = "\u{1f642}".repeat(128 * 1024);
      const result = await bounded(client.request("execute", "rejected-tool", undefined, () => Promise.reject(detail)), "oversized multibyte rejection");
      assert.match(result.error, /exceeds 262144 bytes/);
      await bounded(client.close(), "oversized multibyte rejection close");
      await bounded(child.exit, "oversized multibyte rejection child exit");
    }],
    ["unencodable rejection retires sidecar", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      await bounded(rejects(client.request("execute", "unencodable-rejected-tool", undefined, () => Promise.reject("\\".repeat(512 * 1024))), /frame exceeds 1048576 bytes/), "unencodable rejection failure");
      await bounded(child.exit, "unencodable rejection child exit", 3000);
      assert.equal(child.exited, true, "unencodable rejection must retire its sidecar");
      await bounded(client.close(), "unencodable rejection close");
    }],
    ["exact-cap escaped no-context tool call retires sidecar", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      await bounded(
        rejects(client.request("execute", "exact-cap-no-context-tool"), /frame exceeds 1048576 bytes/),
        "exact-cap no-context rejection",
      );
      await bounded(child.exit, "exact-cap no-context child exit", 3000);
      assert.equal(child.exited, true, "exact-cap no-context failure must retire its sidecar");
      await bounded(client.close(), "exact-cap no-context close");
    }],
    ["exact-cap multibyte no-context tool call retires sidecar", async (make, track) => {
      const client = make("ready");
      const child = track(client.child);
      await bounded(
        rejects(client.request("execute", "exact-cap-multibyte-no-context-tool"), /frame exceeds 1048576 bytes/),
        "exact-cap multibyte no-context rejection",
      );
      await bounded(child.exit, "exact-cap multibyte no-context child exit", 3000);
      assert.equal(child.exited, true, "exact-cap multibyte no-context failure must retire its sidecar");
      await bounded(client.close(), "exact-cap multibyte no-context close");
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
  console.log(`rlm lifecycle checks passed: ${cases.length}/${cases.length}; ${observed} owned children, 0 survivors BEFORE backup`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  await lifecycleChecks(process.cwd());
}
