// Fixed offline lifecycle transport, not inference. Live never loads this file.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function (pi) {
  let cell;
  let continuation = false;
  let requests = 0;
  const cells = JSON.parse(process.env.PI_RLM_LIFECYCLE_CELLS);
  pi.on("before_agent_start", event => {
    cell = cells[event.prompt.split("\n")[0]];
    continuation = false;
  });
  // Exercise real Pi compaction persistence/callbacks, WITHOUT paid summarization.
  pi.on("session_before_compact", event => ({ compaction: {
    summary: "Synthetic offline lifecycle summary; external Python checkpoint retained.",
    firstKeptEntryId: event.preparation.firstKeptEntryId,
    tokensBefore: event.preparation.tokensBefore,
    details: { syntheticLifecycle: true },
  } }));
  pi.registerProvider("lifecycle-local-replay", {
    api: "lifecycle-local-replay-api", baseUrl: "local://no-network",
    apiKey: "local-test-only-not-a-credential",
    models: [{ id: "fixed-python", name: "Offline lifecycle transport (no inference)",
      reasoning: false, input: ["text"], contextWindow: 32768, maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, _context, options) {
      const stream = createAssistantMessageEventStream();
      const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
        content: [], timestamp: Date.now(), stopReason: "pending",
        usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      (async () => {
        try {
          if (typeof cell !== "string" || ++requests > 12) throw Error("Unexpected local request");
          await options?.onPayload?.({ replay: "lifecycle" }, model);
          const emit = async event => {
            await options?.onProviderStreamEvent?.({ replay: "lifecycle", type: event.type }, model);
            if (options?.signal?.aborted) throw Error("Local replay aborted");
            stream.push(event);
          };
          await emit({ type: "start", partial: message });
          if (!continuation) {
            const toolCall = { type: "toolCall", id: "call_" + String(requests).padStart(32, "0"), name: process.env.PI_RLM_LIFECYCLE_NEGATIVE === "direct-read" ? "read" : "python", arguments: {} };
            message.content.push(toolCall);
            await emit({ type: "toolcall_start", contentIndex: 0, partial: message });
            toolCall.arguments = toolCall.name === "read" ? { path: "large.txt" } : { code: cell };
            await emit({ type: "toolcall_delta", contentIndex: 0, delta: JSON.stringify(toolCall.arguments), partial: message });
            await emit({ type: "toolcall_end", contentIndex: 0, toolCall, partial: message });
            message.stopReason = "toolUse";
            continuation = true;
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
          message.errorMessage = "Local lifecycle stream failed";
          stream.push({ type: "error", reason: message.stopReason, error: message });
        } finally { stream.end(); }
      })();
      return stream;
    },
  });
}
