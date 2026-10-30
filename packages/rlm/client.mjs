import { spawn } from "node:child_process";
import { readFileSync, readdirSync } from "node:fs";
import { createInterface } from "node:readline";

const MAX_FRAME_BYTES = 1024 * 1024;
const MAX_PAYLOAD_BYTES = 256 * 1024;

function serializedFrame(message) {
  const frame = JSON.stringify(message);
  if (Buffer.byteLength(frame) > MAX_FRAME_BYTES) throw new Error(`RLM kernel frame exceeds ${MAX_FRAME_BYTES} bytes`);
  return `${frame}\n`;
}

function boundedError(error) {
  try {
    const detail = String(error);
    if (Buffer.byteLength(JSON.stringify(detail)) <= MAX_PAYLOAD_BYTES) return detail;
  } catch {}
  return `pi tool error exceeds ${MAX_PAYLOAD_BYTES} bytes`;
}

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
    this.pendingStateLossNotice = undefined;
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
              child.stdin.write(serializedFrame({ id: this.nextId++, operation: "shutdown" }));
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
        request.barrierReject(reason);
      }
      this.pending.clear();
    };
    this.fail = fail;

    const writeToolResult = (message, request) => {
      try {
        child.stdin.write(serializedFrame(message));
        request?.armTimeout();
        return true;
      } catch (error) {
        fail(error);
        stopChild();
        return false;
      }
    };

    let stdoutBytes = 0;
    createInterface({ input: child.stdout }).on("line", (line) => {
      stdoutBytes = Buffer.byteLength(line);
      if (stdoutBytes > MAX_FRAME_BYTES) {
        fail(new Error(`RLM kernel frame exceeds ${MAX_FRAME_BYTES} bytes`));
        stopChild();
        return;
      }
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
          writeToolResult({ id: message.id, type: "tool_result", generation: message.generation, cell_id: message.cell_id, call_id: message.call_id, error: "No pi tool context is available" });
          return;
        }
        clearTimeout(request.timer);
        Promise.resolve(request.onToolCall(message.name, message.args))
          .then((result) => {
            if (this.child !== child || this.pending.get(message.id) !== request) return;
            if (Buffer.byteLength(JSON.stringify(result)) > MAX_PAYLOAD_BYTES) throw new Error(`pi tool result exceeds ${MAX_PAYLOAD_BYTES} bytes`);
            writeToolResult({ id: message.id, type: "tool_result", generation: message.generation, cell_id: message.cell_id, call_id: message.call_id, result }, request);
          })
          .catch((error) => {
            if (this.child !== child || this.pending.get(message.id) !== request) return;
            writeToolResult({ id: message.id, type: "tool_result", generation: message.generation, cell_id: message.cell_id, call_id: message.call_id, error: boundedError(error) }, request);
          });
        return;
      }
      const request = this.pending.get(message.id);
      if (!request) return;
      clearTimeout(request.timer);
      this.pending.delete(message.id);
      if (message.ok) {
        if (request.aborted && message.result?.state_lost && message.result.notice) {
          this.pendingStateLossNotice = message.result.notice;
        }
        if (!request.aborted) request.resolve(message.result);
        request.barrierResolve(message.result);
      } else {
        const error = new Error(message.error);
        if (!request.aborted) request.reject(error);
        request.barrierReject(error);
      }
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
      if (this.isReady && !this.stopping) this.options.onUnexpectedExit?.();
      fail(new Error(`RLM kernel exited (${code ?? signal ?? "unknown"})${this.stderr ? `: ${this.stderr}` : ""}`));
      stopChild();
    });
  }

  request(operation, code, signal, onToolCall, tools = []) {
    let callerResolve;
    let callerReject;
    const caller = new Promise((resolve, reject) => { callerResolve = resolve; callerReject = reject; });
    caller.catch(() => undefined);
    let activeRequest;
    let runStarted = false;
    let abortSent = false;
    const detach = (request) => {
      if (activeRequest === request) activeRequest = undefined;
      signal?.removeEventListener("abort", abort);
    };
    const abort = () => {
      const error = new Error("RLM kernel request aborted");
      callerReject(error);
      if (activeRequest && !abortSent) {
        abortSent = true;
        activeRequest.aborted = true;
        clearTimeout(activeRequest.timer);
        try {
          this.child.stdin.write(serializedFrame({ id: activeRequest.id, operation: "abort" }));
        } catch (writeError) {
          this.fail(writeError);
          this.stopChild();
        }
      } else if (runStarted) {
        this.fail(error);
        this.stopChild();
      }
    };
    signal?.addEventListener("abort", abort, { once: true });

    const runBarrier = async () => {
      runStarted = true;
      try {
        if (this.stopping) throw new Error("RLM kernel is shutting down");
        if (this.closed) {
          await this.stopChild();
          if (this.stopping) throw new Error("RLM kernel is shutting down");
          this.start();
        }
        if (signal?.aborted) throw new Error("RLM kernel request aborted");
        await this.ready;
        if (this.stopping) throw new Error("RLM kernel is shutting down");
        if (signal?.aborted) throw new Error("RLM kernel request aborted");
        if (this.pendingStateLossNotice) {
          const notice = this.pendingStateLossNotice;
          this.pendingStateLossNotice = undefined;
          throw new Error(notice);
        }
        const id = this.nextId++;
        const barrier = new Promise((barrierResolve, barrierReject) => {
          const request = {
            id,
            resolve: (value) => {
              detach(request);
              callerResolve(value);
            },
            reject: (error) => {
              detach(request);
              callerReject(error);
            },
            barrierResolve,
            barrierReject,
            aborted: false,
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
          activeRequest = request;
          request.armTimeout();
          this.pending.set(id, request);
        });
        this.child.stdin.write(serializedFrame({ id, operation, code, tools }));
        await barrier;
      } catch (error) {
        detach(activeRequest);
        callerReject(error);
        throw error;
      } finally {
        detach(activeRequest);
      }
    };
    const barrier = this.queue.then(runBarrier, runBarrier);
    this.queue = barrier.catch(() => undefined);
    return caller;
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
