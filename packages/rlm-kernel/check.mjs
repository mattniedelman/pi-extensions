import { spawnSync } from "node:child_process";
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { SidecarClient } from "./client.mjs";
import { bounded, lifecycleChecks } from "./lifecycle-check.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const root = await mkdtemp(join(tmpdir(), "pi-rlm-client-"));
let client;
const children = [];
function observeChild() {
  const child = client.child;
  const exit = new Promise((resolve) => child.once("close", resolve));
  children.push({ child, exit });
  return exit;
}
async function identity(pid) {
  try {
    const fields = (await readFile(`/proc/${pid}/stat`, "utf8")).split(")").at(-1).trim().split(/\s+/);
    return { pid, start: fields[19], parent: Number(fields[1]) };
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return null;
    throw error;
  }
}
async function ownedTree(pid) {
  const parent = await identity(pid);
  assert.ok(parent, "real sidecar identity must exist before abort");
  const descendants = new Set();
  for (const task of await readdir(`/proc/${pid}/task`)) {
    for (const child of (await readFile(`/proc/${pid}/task/${task}/children`, "utf8")).trim().split(/\s+/)) {
      if (child) descendants.add(Number(child));
    }
  }
  assert.equal((await identity(pid))?.start, parent.start);
  const result = [parent];
  for (const child of descendants) {
    assert.equal((await identity(child))?.parent, pid);
    result.push(...await ownedTree(child));
  }
  return result;
}
let slowStarted;
const toolCalls = [];
let effects = 0;
const tools = [
  { name: "read", method: "read" },
  { name: "foo-bar", method: null },
];
const onToolCall = async (name, args) => {
  toolCalls.push({ name, args });
  if (name === "fail") return { content: "blocked", structured: null, is_error: true };
  if (name === "effect") { effects += 1; return { content: "changed", structured: null, is_error: false }; }
  if (name === "slow") { slowStarted?.(); await new Promise((resolve) => setTimeout(resolve, 150)); return { content: "late", structured: null, is_error: false }; }
  if (name === "__bridge_tools__") return { content: "", structured: tools, is_error: false };
  if (name === "__bridge_search__") return { content: "", structured: [{ name: "read" }], is_error: false };
  if (name === "__bridge_describe__") return { content: "", structured: { name: args.name }, is_error: false };
  return { content: `content:${name}`, structured: { name, args }, is_error: false };
};
try {
  const stopped = spawnSync("python3", [join(here, "stopped-worker-check.py"), process.execPath], {
    stdio: "inherit", env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  assert.equal(stopped.status, 0, "real stopped-worker teardown regression");
  await lifecycleChecks(root);
  client = new SidecarClient({
    command: "uv",
    args: ["run", "--project", join(here, "python"), "--locked", "python", join(here, "python", "sidecar.py"), "--state", join(root, "state.bin")],
    cwd: root,
    requestTimeoutMs: 200,
    startupTimeoutMs: 5000,
  });
  const firstExit = observeChild();
  const natural = await client.request("execute", "doc = pi.read(path='a.txt')\ndoc['structured']['name']", undefined, onToolCall, tools);
  assert.equal(natural.result, "read");
  const exact = await client.request("execute", "pi.call('foo-bar', {'x': 1})['structured']['args']['x']", undefined, onToolCall, tools);
  assert.equal(exact.result, 1);
  const discovery = await client.request("execute", "[pi.tools()['structured'][0]['name'], pi.search('file')['structured'][0]['name'], pi.describe('read')['structured']['name']]", undefined, onToolCall, tools);
  assert.deepEqual(discovery.result, ["read", "read", "read"]);
  await client.request("execute", "saved = pi.read", undefined, onToolCall, tools);
  const failedTool = await client.request("execute", "temp = 9\npi.call('fail', {})", undefined, onToolCall, tools);
  assert.equal(failedTool.result.is_error, true);
  assert.equal((await client.request("execute", "temp", undefined, onToolCall, tools)).result, 9);
  await assert.rejects(client.request("execute", "pi.call('effect', {})\n1 / 0", undefined, onToolCall, tools), /ZeroDivisionError/);
  assert.equal(effects, 1);
  const alias = await client.request("execute", "saved(path='b.txt')['structured']['args']['path']", undefined, onToolCall, tools);
  assert.equal(alias.result, "b.txt");
  client.child.kill();
  await bounded(firstExit, "real child killed before restart", 3000);
  const restoredAlias = await client.request("execute", "saved(path='restored.txt')['structured']['args']['path']", undefined, onToolCall, tools);
  assert.equal(restoredAlias.result, "restored.txt");
  observeChild();
  const [a, b] = await Promise.all([
    client.request("execute", "x = 1\nx", undefined, onToolCall, tools),
    client.request("execute", "x += 1\nx", undefined, onToolCall, tools),
  ]);
  assert.equal(a.result, 1);
  assert.equal(b.result, 2);
  const nestedController = new AbortController();
  const nestedStarted = new Promise((resolve) => { slowStarted = resolve; });
  const nestedInterrupted = client.request("execute", "pi.call('slow', {})", nestedController.signal, onToolCall, tools);
  const nestedRejected = assert.rejects(nestedInterrupted, /aborted/);
  nestedRejected.catch(() => undefined);
  await bounded(nestedStarted, "real nested call started", 1000);
  const oldTree = await ownedTree(client.child.pid);
  assert.ok(oldTree.length >= 3, "observe real UV/Python/Monty before abort");
  nestedController.abort();
  await nestedRejected;
  assert.equal((await bounded(client.request("execute", "x", undefined, onToolCall, tools), "real lazy restart under supervisor", 4500)).result, 2);
  for (const old of oldTree) {
    assert.notEqual((await identity(old.pid))?.start, old.start, "old identity must be absent before backup");
  }
  console.log(`REAL nested abort/lazy restart: ${oldTree.length} old identities absent BEFORE backup`);
  observeChild();
  await new Promise((resolve) => setTimeout(resolve, 400));
  assert.equal((await client.request("execute", "x", undefined, onToolCall, tools)).result, 2);
  const controller = new AbortController();
  const interrupted = client.request("execute", "while True:\n    pass", controller.signal, onToolCall, tools);
  setTimeout(() => controller.abort(), 50);
  await assert.rejects(interrupted, /aborted/);
  const restored = await client.request("execute", "x", undefined, onToolCall, tools);
  assert.equal(restored.result, 2);
  observeChild();
  await client.close();
  assert.equal(client.stopping, true);
  for (const { child, exit } of children) {
    await bounded(exit, "real sidecar child exit", 3000);
    assert.throws(() => process.kill(child.pid, 0), { code: "ESRCH" });
  }
  console.log(`rlm-kernel real client cleanup passed: ${children.length} owned children, 0 survivors`);
  const missing = new SidecarClient({ command: join(root, "missing-uv"), args: [], cwd: root });
  try {
    await assert.rejects(missing.request("execute", "1 + 1"), /ENOENT|not running/);
  } finally {
    await missing.close();
  }
} finally {
  await client?.close();
  await rm(root, { recursive: true, force: true });
}
await assert.rejects(stat(root), { code: "ENOENT" });
console.log("rlm-kernel client checks passed");
