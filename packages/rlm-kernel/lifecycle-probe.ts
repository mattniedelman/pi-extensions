// Test-only observation/guard command. Never imports/replaces the RLM factory.
import { appendFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

export default function (pi) {
  const generation = randomUUID();
  const cells = JSON.parse(process.env.PI_RLM_LIFECYCLE_CELLS);
  const log = record => appendFileSync(process.env.PI_RLM_LIFECYCLE_LOG, JSON.stringify(record) + "\n", { mode: 0o600 });
  let expected;
  let calls = 0;
  let responses = 0;
  pi.on("session_start", (_event, ctx) => log({ type: "start", generation, sessionFile: ctx.sessionManager.getSessionFile() }));
  pi.registerCommand("lifecycle-compact-boundary", { description: "Bounded synthetic transcript boundary (no model call)", handler: async () => {
    pi.sendMessage({ customType: "lifecycle-fixture", content: "Bounded lifecycle context. ".repeat(100), display: false });
  } });
  pi.registerCommand("lifecycle-reload", { description: "Temporary lifecycle smoke reload", handler: async (_args, ctx) => {
    await ctx.reload();
  } });
  pi.on("before_agent_start", event => {
    const phase = event.prompt.split("\n")[0];
    expected = cells[phase];
    calls = 0;
    responses = 0;
    if (typeof expected !== "string") throw Error("Unexpected lifecycle phase");
  });
  // At most one tool response and one final response per controlled prompt.
  pi.on("message_end", (event, ctx) => {
    if (event.message.role === "assistant" && ++responses >= 2 && event.message.stopReason !== "stop") ctx.abort();
  });
  pi.on("tool_call", event => {
    const match = event.toolName === "python"
      ? ++calls === 1 && event.input?.code === expected
        && (event.input.operation ?? "execute") === "execute"
        && Object.keys(event.input).every(key => ["code", "operation"].includes(key))
      : event.toolName === "read" && expected === cells["LIFECYCLE seed"]
        && JSON.stringify(event.input) === JSON.stringify({ path: "large.txt" });
    log({ type: "call", toolName: event.toolName === "python" ? "python" : event.toolName === "read" ? "read" : "other",
      id: event.toolCallId, match });
    if (!match) return { block: true, reason: "Unexpected lifecycle tool call" };
  });
  pi.on("tool_result", event => log({ type: "result", toolName: event.toolName === "python" ? "python" : "read",
    id: event.toolCallId, isError: event.isError }));
  pi.on("session_before_compact", event => {
    log({ type: "compact", reason: event.reason,
      keepRecentTokens: event.preparation.settings.keepRecentTokens,
      reserveTokens: event.preparation.settings.reserveTokens,
      splitTurn: event.preparation.isSplitTurn });
    // Do not consume two paid summary requests if fixture preparation changes.
    if (event.preparation.isSplitTurn) return { cancel: true };
  });
}
