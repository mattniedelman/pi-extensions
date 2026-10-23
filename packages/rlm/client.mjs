import { spawn } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { createInterface } from "node:readline";

function processInfo(pid) {
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    const fields = stat.slice(stat.lastIndexOf(")") + 1).trim().split(/\s+/);
    return { start: fields[19], parent: Number(fields[1]), group: Number(fields[2]), session: Number(fields[3]) };
  } catch (error) {
    if (error.code === "ENOENT" || error.code === "ESRCH") return null;
    throw error;
  }
}

// Linux sidecar scope only: known child edges, never the global PID table or
// caller's children. The private group also covers fast forks within that group.
// /proc start checks reduce PID reuse risk; Node kill is NOT an atomic pidfd send.
function sidecarTree(child) {
  const known = new Map();
  const root = child.pid && processInfo(child.pid);
  if (root) known.set(child.pid, root.start);
  const observe = () => {
    const pending = [...known];
    const visited = new Set();
    while (pending.length) {
      const [pid, start] = pending.pop();
      const before = processInfo(pid);
      if (!before || before.start !== start || visited.has(pid)) continue;
      visited.add(pid);
      const descendants = new Set();
      try {
        for (const task of readdirSync(`/proc/${pid}/task`)) {
          try {
            for (const value of readFileSync(`/proc/${pid}/task/${task}/children`, "utf8").trim().split(/\s+/)) {
              if (value) descendants.add(Number(value));
            }
          } catch (error) {
            if (error.code !== "ENOENT" && error.code !== "ESRCH") throw error;
          }
        }
      } catch (error) {
        if (error.code !== "ENOENT" && error.code !== "ESRCH") throw error;
      }
      if (processInfo(pid)?.start !== start) continue;
      for (const descendant of descendants) {
        const info = processInfo(descendant);
        if (info?.parent !== pid || processInfo(pid)?.start !== start) continue;
        known.set(descendant, info.start);
        pending.push([descendant, info.start]);
      }
    }
  };
  const remaining = () => {
    observe();
    return [...known].flatMap(([pid, start]) => {
      const info = processInfo(pid);
      return info?.start === start ? [{ pid, ...info }] : [];
    });
  };
  const send = (signal) => {
    const members = remaining();
    // An observed live identity anchors the private SID/PGID even after uv exits.
    // Never authorize an old group ID merely because kill(group, 0) succeeds.
    const anchor = members.find((info) => info.group === child.pid && info.session === child.pid);
    const current = anchor && processInfo(anchor.pid);
    let grouped = false;
    if (current && current.start === anchor.start && current.group === child.pid && current.session === child.pid) {
      try { process.kill(-child.pid, signal); grouped = true; }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    for (const member of members) {
      if (grouped && member.group === child.pid && member.session === child.pid) continue;
      if (processInfo(member.pid)?.start !== member.start) continue;
      try { process.kill(member.pid, signal); }
      catch (error) { if (error.code !== "ESRCH") throw error; }
    }
  };
  return { observe, remaining, send };
}

async function waitForRetirement(exit, timeoutMs, remaining) {
  let closed = false;
  exit.then(() => { closed = true; });
  const deadline = performance.now() + timeoutMs;
  do {
    if (closed && !remaining()) return true;
    await new Promise((resolve) => setTimeout(resolve, 10));
  } while (performance.now() < deadline);
  return closed && !remaining();
}

export class SidecarClient {
  constructor(options) {
    this.options = { env: process.env, requestTimeoutMs: 10000, startupTimeoutMs: 120000, ...options };
    this.pending = new Map();
    this.nextId = 1;
    this.queue = Promise.resolve();
    this.stopping = false;
    this.start();
  }

  start() {
    const { command, args, cwd, env, startupTimeoutMs } = this.options;
    this.closed = false;
    this.isReady = false;
    this.stderr = "";
    const linux = process.platform === "linux";
    const child = spawn(command, args, { cwd, env, detached: linux, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    const tree = linux ? sidecarTree(child) : null;
    let observationError;
    const watch = tree && setInterval(() => {
      try { tree.observe(); }
      catch (error) { observationError = error; }
    }, 25);
    const exit = new Promise((resolve) => child.once("close", resolve));
    const remaining = () => tree ? tree.remaining().length : false;
    const send = (signal) => tree ? tree.send(signal) : child.kill(signal);
    let termination;
    const stopChild = this.stopChild = (graceful = false) => {
      if (!termination) {
        termination = (async () => {
          try {
            if (graceful) {
              child.stdin.write(`${JSON.stringify({ id: this.nextId++, operation: "shutdown" })}\n`);
            } else {
              send("SIGTERM");
            }
            if (!await waitForRetirement(exit, 2000, remaining)) {
              send("SIGKILL");
              if (!await waitForRetirement(exit, 2000, remaining)) throw new Error("RLM kernel owned tree did not exit after SIGKILL");
            }
            if (observationError) throw observationError;
          } finally {
            clearInterval(watch);
          }
        })();
        // Timer/abort handlers initiate cleanup; close/restart also await it.
        termination.catch(() => undefined);
      }
      return termination;
    };
    this.ready = new Promise((resolve, reject) => {
      this.readyResolve = resolve;
      this.readyReject = reject;
      this.readyTimer = setTimeout(() => {
        fail(new Error(`RLM kernel startup timed out after ${startupTimeoutMs}ms`));
        stopChild();
      }, startupTimeoutMs);
    });
    this.ready.catch(() => undefined);

    const fail = (error) => {
      if (this.child !== child) return;
      this.closed = true;
      clearTimeout(this.readyTimer);
      const reason = error instanceof Error ? error : new Error(String(error));
      this.readyReject?.(reason);
      for (const request of this.pending.values()) {
        clearTimeout(request.timer);
        request.reject(reason);
      }
      this.pending.clear();
    };
    this.fail = fail;

    createInterface({ input: child.stdout }).on("line", (line) => {
      if (this.child !== child || this.closed) return;
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        fail(new Error(`RLM kernel returned invalid JSON: ${line.slice(0, 200)}`));
        stopChild();
        return;
      }
      if (message.ready) {
        this.isReady = true;
        clearTimeout(this.readyTimer);
        this.readyResolve();
        return;
      }
      if (message.type === "tool_call") {
        const request = this.pending.get(message.id);
        if (!request?.onToolCall) {
          child.stdin.write(`${JSON.stringify({ id: message.id, type: "tool_result", call_id: message.call_id, error: "No pi tool context is available" })}\n`);
          return;
        }
        clearTimeout(request.timer);
        Promise.resolve(request.onToolCall(message.name, message.args))
          .then((result) => {
            if (this.child !== child || this.pending.get(message.id) !== request) return;
            child.stdin.write(`${JSON.stringify({ id: message.id, type: "tool_result", call_id: message.call_id, result })}\n`);
            request.armTimeout();
          })
          .catch((error) => {
            if (this.child !== child || this.pending.get(message.id) !== request) return;
            child.stdin.write(`${JSON.stringify({ id: message.id, type: "tool_result", call_id: message.call_id, error: String(error) })}\n`);
            request.armTimeout();
          });
        return;
      }
      const request = this.pending.get(message.id);
      if (!request) return;
      clearTimeout(request.timer);
      this.pending.delete(message.id);
      if (message.ok) request.resolve(message.result);
      else request.reject(new Error(message.error));
    });
    child.stderr.on("data", (chunk) => {
      this.stderr = (this.stderr + chunk.toString()).slice(-8192);
    });
    const childError = (error) => {
      fail(error);
      stopChild();
    };
    child.stdin.on("error", childError);
    child.on("error", childError);
    child.on("exit", (code, signal) => {
      fail(new Error(`RLM kernel exited (${code ?? signal ?? "unknown"})${this.stderr ? `: ${this.stderr}` : ""}`));
      stopChild();
    });
  }

  request(operation, code, signal, onToolCall, tools = []) {
    const run = async () => {
      if (this.stopping) throw new Error("RLM kernel is shutting down");
      if (this.closed) {
        await this.stopChild();
        if (this.stopping) throw new Error("RLM kernel is shutting down");
        this.start();
      }
      const abort = () => {
        this.fail(new Error("RLM kernel request aborted"));
        this.stopChild();
      };
      signal?.addEventListener("abort", abort, { once: true });
      try {
        if (signal?.aborted) abort();
        await this.ready;
        if (this.stopping) throw new Error("RLM kernel is shutting down");
        if (signal?.aborted) throw new Error("RLM kernel request aborted");
        const id = this.nextId++;
        const response = new Promise((resolve, reject) => {
          const request = {
            resolve,
            reject,
            timer: undefined,
            onToolCall,
            armTimeout: () => {
              clearTimeout(request.timer);
              request.timer = setTimeout(() => {
                this.fail(new Error(`RLM kernel request timed out after ${this.options.requestTimeoutMs}ms`));
                this.stopChild();
              }, this.options.requestTimeoutMs);
            },
          };
          request.armTimeout();
          this.pending.set(id, request);
        });
        this.child.stdin.write(`${JSON.stringify({ id, operation, code, tools })}\n`);
        return await response;
      } finally {
        signal?.removeEventListener("abort", abort);
      }
    };
    const result = this.queue.then(run, run);
    this.queue = result.catch(() => undefined);
    return result;
  }

  close() {
    if (this.closing) return this.closing;
    this.stopping = true;
    const graceful = this.isReady && !this.closed && !this.pending.size;
    this.fail(new Error("RLM kernel is shutting down"));
    this.closing = this.stopChild(graceful);
    return this.closing;
  }
}
