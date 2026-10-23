import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { SidecarClient } from "./client.mjs";
import { handleBridgeCall, toolCatalog } from "./bridge.mjs";
import { renderPythonCall, renderPythonResult } from "./render.ts";

const here = dirname(fileURLToPath(import.meta.url));

export default function (pi: ExtensionAPI) {
  let client: SidecarClient | undefined;

  pi.on("session_start", async (_event, ctx) => {
    await client?.close();
    const identity = ctx.sessionManager.getSessionFile() ?? `${ctx.cwd}\0${ctx.sessionManager.getSessionId()}`;
    const key = createHash("sha256").update(identity).digest("hex");
    const root = process.env["PI_RLM_STATE_DIR"] ?? join(homedir(), ".pi", "agent", "rlm");
    const state = join(root, key, "kernel.bin");
    client = new SidecarClient({
      command: "uv",
      args: ["run", "--project", join(here, "python"), "--locked", "python", join(here, "python", "sidecar.py"), "--state", state],
      cwd: ctx.cwd,
      env: { ...process.env, UV_PROJECT_ENVIRONMENT: join(root, ".venv") },
    });
  });

  pi.registerTool({
    name: "python",
    label: "Python",
    description: "Run Python-subset code in a persistent, sandboxed Monty session. Globals survive across calls and resumed pi sessions. Python can call current pi tools naturally, for example pi.read(path='README.md') or pi.bash(command='git status'). Use pi.call(name, args) for exact tool names and pi.tools(), pi.search(query), or pi.describe(name) for discovery. Tool effects are real even if later Python code fails. Other operations: variables, reset, checkpoint.",
    exposure: "model-only",
    renderCall: renderPythonCall,
    renderResult: renderPythonResult,
    parameters: Type.Object({
      operation: Type.Optional(Type.Union([
        Type.Literal("execute"),
        Type.Literal("variables"),
        Type.Literal("reset"),
        Type.Literal("checkpoint"),
      ])),
      code: Type.Optional(Type.String({ description: "Code to run for the execute operation" })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!client) throw new Error("RLM kernel has not started");
      const operation = params.operation ?? "execute";
      if (operation === "execute" && (typeof params.code !== "string" || !params.code)) {
        throw new Error("code is required for execute and must be a non-empty string");
      }
      if (operation !== "execute") {
        const result = await client.request(operation, params.code, signal);
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
      }
      if (typeof ctx?.executeTool !== "function") throw new Error("Nested pi tools are not available in this context");
      if (!Array.isArray(ctx.tools)) throw new Error("Callable pi tools are not available in this context");
      const catalog = toolCatalog(pi, ctx);
      const result = await client.request(
        operation,
        params.code,
        signal,
        (name, args) => handleBridgeCall(pi, ctx, name, args, signal),
        catalog.map(({ name, method }) => ({ name, method })),
      );
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  pi.on("session_shutdown", async () => {
    const current = client;
    client = undefined;
    await current?.close();
  });
}
