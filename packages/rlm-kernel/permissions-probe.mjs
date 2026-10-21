import { appendFileSync } from "node:fs";

// Observe real hooks only; never replace builtin read or fabricate its events.
export default function (pi) {
  const log = (record) => appendFileSync(process.env.PI_RLM_POLICY_LOG,
    JSON.stringify(record) + "\n", { mode: 0o600 });
  pi.on("tool_call", (event) => {
    log({ type: "tool_call", toolName: event.toolName, toolCallId: event.toolCallId, input: event.input });
    if (event.toolName === "python") return;
    if (event.toolName === "read" && /\/\d+$/.test(event.toolCallId)) {
      if (JSON.stringify(event.input) === JSON.stringify({ path: "allowed.txt" })) return;
      if (JSON.stringify(event.input) === JSON.stringify({ path: "denied.txt" })) {
        return { block: true, reason: "IUA_DENIED" };
      }
    }
    return { block: true, reason: "IUA_UNEXPECTED_TOOL" };
  });
  pi.on("tool_result", (event) => {
    // No file contents/error strings are written to this policy log.
    log({ type: "tool_result", toolName: event.toolName, toolCallId: event.toolCallId,
      input: event.input, isError: event.isError });
  });
}
