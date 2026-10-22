// Test-only local stream transport, NOT inference or model-quality evidence.
import { readFileSync } from "node:fs";
import { createAssistantMessageEventStream, getCurrentTools } from "@earendil-works/pi-ai";
import policy from "./permissions-probe.mjs";

export default function (pi) {
  const scenario = process.env.PI_RLM_REPLAY_CASE;
  if (!["good", "readBeforeDeny", "lostHook"].includes(scenario)) throw Error("Unknown replay fixture");
  // Register real core handlers. Negatives alter policy, never execution events.
  policy({ on: (name, handler) => pi.on(name, (event) => {
    if (name === "tool_call" && event.toolName === "read" && event.input?.path === "denied.txt" && scenario === "readBeforeDeny") {
      readFileSync("denied.txt"); // Discard bytes: the inode sensor must still fail.
    }
    const result = handler(event);
    return name === "tool_call" && scenario === "lostHook" ? undefined : result;
  }) });
  let requests = 0;
  pi.registerProvider("q1s-local-replay", {
    api: "q1s-local-replay-api", baseUrl: "local://q1s-no-network",
    apiKey: "q1s-test-only-not-a-credential",
    models: [{ id: "fixed-python", name: "Q1S local transport replay (no inference)",
      reasoning: false, input: ["text"], contextWindow: 32768, maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, context, options) {
      const stream = createAssistantMessageEventStream();
      const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
        content: [], timestamp: Date.now(), stopReason: "pending",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      (async () => {
        try {
          if (options?.signal?.aborted) throw Error("Replay aborted");
          if (++requests > 2) throw Error("Unexpected replay continuation");
          const tools = getCurrentTools(context.messages);
          if (!["python", "read"].every(name => tools.some(tool => tool.name === name))) throw Error("Replay tools missing");
          // No outbound request/response exists. Honor payload and stream instrumentation locally.
          await options?.onPayload?.({ replay: "fixed-python", request: requests }, model);
          const emit = async (event) => {
            await options?.onProviderStreamEvent?.({ replay: "fixed-python", type: event.type }, model);
            stream.push(event);
          };
          await emit({ type: "start", partial: message });
          if (requests === 1) {
            const toolCall = { type: "toolCall", id: "call_" + "q".repeat(32), name: "python", arguments: {} };
            message.content.push(toolCall);
            await emit({ type: "toolcall_start", contentIndex: 0, partial: message });
            toolCall.arguments = { code: process.env.PI_RLM_EXPECTED_CELL };
            await emit({ type: "toolcall_delta", contentIndex: 0, delta: JSON.stringify(toolCall.arguments), partial: message });
            await emit({ type: "toolcall_end", contentIndex: 0, toolCall, partial: message });
            message.stopReason = "toolUse";
          } else {
            message.content.push({ type: "text", text: "" });
            await emit({ type: "text_start", contentIndex: 0, partial: message });
            message.content[0].text = "DONE";
            await emit({ type: "text_delta", contentIndex: 0, delta: "DONE", partial: message });
            await emit({ type: "text_end", contentIndex: 0, content: "DONE", partial: message });
            message.stopReason = "stop";
          }
          await emit({ type: "done", reason: message.stopReason, message });
        } catch {
          message.stopReason = options?.signal?.aborted ? "aborted" : "error";
          message.errorMessage = "Local replay transport failed";
          stream.push({ type: "error", reason: message.stopReason, error: message });
        } finally {
          stream.end();
        }
      })();
      return stream;
    },
  });
}
