import assert from "node:assert/strict";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { SidecarClient } from "./client.mjs";
import { bounded } from "./lifecycle-check.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const [mode, root] = process.argv.slice(2);
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function info(pid) {
  try {
    const stat = await readFile(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
    return { pid, start: fields[19], parent: Number(fields[1]), group: Number(fields[2]), session: Number(fields[3]), state: fields[0] };
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return null;
    throw error;
  }
}
const record = join(root, `${mode}.json`);
const normalArgs = ["run", "--project", join(here, "python"), "--locked", "python", join(here, "python", "sidecar.py"), "--state", join(root, "kernel.bin")];
const client = new SidecarClient({
  command: "uv", cwd: root,
  args: [...normalArgs.slice(0, 5), join(here, "python", "stopped_worker_fixture.py"), mode, record, join(root, "kernel.bin")],
  startupTimeoutMs: 1200, requestTimeoutMs: 100,
});
const uv = await info(client.child.pid);
const controller = new AbortController();
const request = client.request("execute", "1", controller.signal);
const rejected = assert.rejects(request, mode === "startup-abort" ? /aborted/ : mode === "startup-close" ? /shutting down/ : /timed out/);
rejected.catch(() => undefined);
const failures = [];
let tree;
let recording = true;
try {
  tree = await bounded((async () => {
    while (recording) {
      try { return JSON.parse(await readFile(record, "utf8")); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
      await delay(5);
    }
  })(), "stopped worker record", 1000);
  assert.equal(tree.python.parent, uv.pid, "Python must be a child of our uv");
  assert.equal(uv.group, uv.pid, "uv must lead a private group");
  assert.equal(uv.session, uv.pid, "uv must lead a private session");
  assert.equal(tree.python.group, uv.group);
  assert.equal(tree.python.session, uv.session);
  assert.equal((await info(uv.pid)).start, uv.start);
  assert.equal((await info(tree.python.pid)).start, tree.python.start);
  assert.ok(tree.workers.length);
  for (const worker of tree.workers) {
    assert.equal(worker.parent, tree.python.pid);
    assert.equal(worker.group, uv.group);
    assert.equal(worker.session, uv.session);
    assert.equal((await info(worker.pid)).start, worker.start);
    assert.equal((await info(worker.pid)).state, "T");
  }
  console.log(`OWNED ${mode}: ${JSON.stringify([uv, tree.python, ...tree.workers])}`);
  if (mode === "startup-abort") controller.abort();
  if (mode === "startup-close") {
    assert.equal(client.close(), client.close(), "concurrent close must share retirement");
    await bounded(client.close(), "stopped startup close", 4500);
  }
  await bounded(rejected, "meaningful request rejection", 1500);
  if (mode !== "startup-close") {
    // A failed old tree must retire before the next request can lazily start.
    await writeFile(record, JSON.stringify({ uv, ...tree }));
    client.options.args = [...normalArgs.slice(0, 5), join(here, "python", "stopped_worker_fixture.py"), "restart", record, join(root, "kernel.bin")];
    const restart = client.request("execute", "40 + 2");
    restart.catch(() => undefined);
    const result = await bounded(restart, "real lazy restart", 4500);
    assert.equal(result.result, 42);
    assert.match(client.stderr, /old UV\/Python\/Monty absent before new Kernel/);
    assert.notEqual(client.child.pid, uv.pid);
  }
} catch (error) {
  failures.push(error.message);
} finally {
  recording = false;
  try { await bounded(client.close(), "final close", 4500); }
  catch (error) { failures.push(error.message); }
  // No backup signals here. The separate positive-ancestry subreaper owns that.
  if (tree) {
    const identities = [uv, tree.python, ...tree.workers];
    const survivors = [];
    for (const identity of identities) {
      const current = await info(identity.pid);
      if (current?.start === identity.start) survivors.push(current);
    }
    console.log(`ABSENCE BEFORE backup ${mode}: ${JSON.stringify(survivors)}`);
    if (survivors.length) failures.push("owned UV/Python/Monty identities survived");
  }
}
assert.deepEqual(failures, [], `${mode} counterexamples`);
