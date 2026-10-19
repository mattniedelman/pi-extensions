import { spawn } from "node:child_process";
import { createInterface } from "node:readline";

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
    this.stderr = "";
    const child = spawn(command, args, { cwd, env, stdio: ["pipe", "pipe", "pipe"] });
    this.child = child;
    this.ready = new Promise((resolve, reject) => {
      this.readyResolve = resolve;
      this.readyReject = reject;
      this.readyTimer = setTimeout(() => {
        reject(new Error(`RLM kernel startup timed out after ${startupTimeoutMs}ms`));
        child.kill();
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

    createInterface({ input: child.stdout }).on("line", (line) => {
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        fail(new Error(`RLM kernel returned invalid JSON: ${line.slice(0, 200)}`));
        child.kill();
        return;
      }
      if (message.ready) {
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
      if (this.closed) this.start();
      await this.ready;
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
              this.pending.delete(id);
              reject(new Error(`RLM kernel request timed out after ${this.options.requestTimeoutMs}ms`));
              this.closed = true;
              this.child.kill();
            }, this.options.requestTimeoutMs);
          },
        };
        request.armTimeout();
        this.pending.set(id, request);
      });
      const abort = () => {
        const request = this.pending.get(id);
        if (!request) return;
        clearTimeout(request.timer);
        this.pending.delete(id);
        request.reject(new Error("RLM kernel request aborted"));
        this.closed = true;
        this.child.kill();
      };
      signal?.addEventListener("abort", abort, { once: true });
      try {
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

  async close() {
    if (this.stopping) return;
    this.stopping = true;
    if (this.closed) return;
    const exit = new Promise((resolve) => this.child.once("exit", resolve));
    if (this.pending.size) {
      this.child.kill();
      await Promise.race([exit, new Promise((resolve) => setTimeout(resolve, 2000))]);
      return;
    }
    try {
      await this.ready;
      const id = this.nextId++;
      this.child.stdin.write(`${JSON.stringify({ id, operation: "shutdown" })}\n`);
      await Promise.race([exit, new Promise((resolve) => setTimeout(resolve, 2000))]);
    } catch {
      // Shutdown remains best-effort; session teardown must not fail.
    } finally {
      if (!this.closed) this.child.kill();
    }
  }
}
