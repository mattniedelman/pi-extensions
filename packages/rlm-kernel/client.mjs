import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

async function waitForExit(exit, timeoutMs) {
  let timer;
  try {
    return await Promise.race([
      exit.then(() => true),
      new Promise((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
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
    const child = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    const exit = new Promise((resolve) => child.once("close", resolve));
    let termination;
    this.stopChild = (graceful = false) => {
      if (!termination) {
        termination = (async () => {
          if (graceful) {
            child.stdin.write(`${JSON.stringify({ id: this.nextId++, operation: "shutdown" })}\n`);
          } else {
            child.kill();
          }
          if (!await waitForExit(exit, 2000)) {
            child.kill("SIGKILL");
            if (!await waitForExit(exit, 2000)) throw new Error("RLM kernel did not exit after SIGKILL");
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
        this.stopChild();
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
        this.stopChild();
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
    child.stdin.on("error", fail);
    child.on("error", fail);
    child.on("exit", (code, signal) => {
      fail(new Error(`RLM kernel exited (${code ?? signal ?? "unknown"})${this.stderr ? `: ${this.stderr}` : ""}`));
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
