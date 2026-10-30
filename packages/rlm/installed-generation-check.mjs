import assert from "node:assert/strict";
import { createInstalledGeneration } from "./installed-generation.mjs";

const sidecar = "/installed/packages/rlm/python/sidecar.py";
const root = { pid: 10, start: "100", parent: 1, argv: ["uv", "run", sidecar] };
const transient = { pid: 11, start: "101", parent: 10, argv: [] };
const ready = { ...transient, argv: ["python", sidecar] };
const snapshots = [
  [root, transient],
  [root, ready],
  [root, ready],
];
const installedGeneration = createInstalledGeneration(sidecar, () => snapshots.shift() ?? [root, ready]);

assert.equal(installedGeneration(), null, "transient empty argv must not be ready");
assert.equal(installedGeneration(), null, "first complete observation must establish stability");
assert.deepEqual(installedGeneration(), [root, ready], "stable populated installed generation must become ready");
console.log("installed generation readiness: transient empty argv rejected, stable installed argv passed");
