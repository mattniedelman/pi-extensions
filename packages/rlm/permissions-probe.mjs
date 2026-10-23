import { appendFileSync } from "node:fs";

const validId = (id) => typeof id === "string" &&
  /^call_[A-Za-z0-9]{16,64}(?:\|fc_(?:call_)?[A-Za-z0-9]{16,64})?(?:\/[1-9][0-9]{0,5})?$/.test(id);
const fixturePath = (input) => input && Object.keys(input).length === 1 &&
  ["allowed.txt", "denied.txt"].includes(input.path) ? input.path : undefined;

// Observe real hooks only; never replace builtin read or fabricate its events.
export default function (pi) {
  let parent;
  const log = (type, event, denialReason) => {
    const path = fixturePath(event.input);
    const known = ["python", "read"].includes(event.toolName);
    const argumentMatches = event.toolName === "python"
      ? typeof process.env.PI_RLM_EXPECTED_CELL === "string" && event.input?.code === process.env.PI_RLM_EXPECTED_CELL &&
        (event.input.operation ?? "execute") === "execute" && Object.keys(event.input).every(k => ["code", "operation"].includes(k))
      : event.toolName === "read" && path !== undefined;
    const idMatches = validId(event.toolCallId) && (event.toolName === "python"
      ? event.toolCallId === parent : event.toolCallId === parent + (path === "allowed.txt" ? "/1" : "/2"));
    appendFileSync(process.env.PI_RLM_POLICY_LOG, JSON.stringify({ type,
      toolName: known ? event.toolName : "other", argumentMatches, idMatches,
      ...(validId(event.toolCallId) ? { toolCallId: event.toolCallId } : {}),
      ...(path ? { path } : {}),
      ...(type === "tool_call" ? { denialReason } : { isError: typeof event.isError === "boolean" ? event.isError : null })
    }) + "\n", { mode: 0o600 });
  };
  pi.on("tool_call", (event) => {
    if (event.toolName === "python" && parent === undefined) parent = event.toolCallId;
    let reason = "IUA_UNEXPECTED_TOOL";
    if (event.toolName === "python") reason = "none";
    if (event.toolName === "read" && /\/\d+$/.test(event.toolCallId)) {
      const path = fixturePath(event.input);
      if (path === "allowed.txt") reason = "none";
      if (path === "denied.txt") reason = "IUA_DENIED";
    }
    log("tool_call", event, reason);
    if (reason !== "none") return { block: true, reason };
  });
  pi.on("tool_result", (event) => log("tool_result", event));
}
