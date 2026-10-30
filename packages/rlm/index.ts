import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { SidecarClient } from "./client.mjs";
import { handleBridgeCall, toolCatalog } from "./bridge.mjs";
import { renderPythonCall, renderPythonResult } from "./render.ts";

const here = dirname(fileURLToPath(import.meta.url));
const FRESH_NOTICE = "Python workspace started fresh; state from an earlier process is not restored";
const CHECKPOINT_ERROR = "Python checkpoints are unsupported; state lasts only for the live pi session.";
const INHERITED_ENV = [
  "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "TEMP", "TMP", "HOME",
  "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
  "JUPYTER_CONFIG_DIR", "JUPYTER_DATA_DIR", "JUPYTER_RUNTIME_DIR", "IPYTHONDIR", "UV_CACHE_DIR",
] as const;
const STATE_BEARING = new Set(["message", "compaction", "branch_summary", "custom", "custom_message"]);

function sidecarEnvironment(source: NodeJS.ProcessEnv = process.env) {
  const env: Record<string, string> = {};
  for (const key of INHERITED_ENV) if (source[key] !== undefined) env[key] = source[key]!;
  if (source.UV_OFFLINE === "1") env.UV_OFFLINE = "1";
  env.UV_PROJECT_ENVIRONMENT = source.PI_RLM_ENV_DIR
    ?? join(source.XDG_CACHE_HOME ?? join(source.HOME ?? homedir(), ".cache"), "pi", "rlm", "python-3.13");
  return env;
}

export default function (pi: ExtensionAPI) {
  let client: SidecarClient | undefined;
  let pendingNotice: string | undefined;

  pi.on("session_start", async (event: any, ctx) => {
    const replacedLiveClient = Boolean(client);
    await client?.close();
    const reason = event?.reason;
    const branch = ctx.sessionManager.getBranch?.() ?? [];
    const stateBearingStartup = reason === "startup" && branch.some((entry: any) => STATE_BEARING.has(entry?.type));
    const shouldNotice = replacedLiveClient
      || reason === "reload" || reason === "resume" || reason === "fork"
      || (reason === "new" && Boolean(event?.previousSessionFile))
      || stateBearingStartup;
    pendingNotice = shouldNotice ? FRESH_NOTICE : undefined;
    client = new SidecarClient({
      command: "uv",
      args: ["run", "--project", join(here, "python"), "--locked", "python", join(here, "python", "sidecar.py")],
      cwd: ctx.cwd,
      env: sidecarEnvironment(),
      onUnexpectedExit: () => { pendingNotice = FRESH_NOTICE; },
    });
  });

  pi.registerTool({
    name: "python",
    label: "Python",
    description: "Run Python code in a persistent session-scoped CPython workspace. Globals survive across calls in the live pi process, but are not restored after process or session replacement. Python can call current pi tools naturally, for example pi.read(path='README.md') or pi.bash(command='git status'). Use pi.call(name, args) for exact tool names and pi.tools(), pi.search(query), or pi.describe(name) for discovery. Other operations: variables and reset; checkpoint is unsupported.",
    exposure: "model-only",
    renderCall: renderPythonCall,
    renderResult: renderPythonResult,
    parameters: Type.Object({
      operation: Type.Optional(Type.Union([
        Type.Literal("execute"), Type.Literal("variables"), Type.Literal("reset"), Type.Literal("checkpoint"),
      ])),
      code: Type.Optional(Type.String({ description: "Code to run for the execute operation" })),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      if (!client) throw new Error("RLM kernel has not started");
      if (pendingNotice) {
        const notice = pendingNotice;
        pendingNotice = undefined;
        throw new Error(notice);
      }
      const operation = params.operation ?? "execute";
      if (operation === "checkpoint") throw new Error(CHECKPOINT_ERROR);
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
        operation, params.code, signal,
        (name, args) => handleBridgeCall(pi, ctx, name, args, signal),
        catalog.map(({ name, method }) => ({ name, method })),
      );
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  pi.on("session_shutdown", async () => {
    const current = client;
    client = undefined;
    pendingNotice = undefined;
    await current?.close();
  });
}
