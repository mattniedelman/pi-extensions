// Pure prompt-cache injection for AWS Bedrock ConverseStream payloads.
// The running prime-agent binary's built-in gate only emits cachePoint blocks
// for Claude 4.x / 3.7 / 3.5 ids, so Claude 5 (opus-5, sonnet-5, ...) is billed
// full price every turn. This adds the missing cache points, and no-ops when the
// built-in already added them (so Claude 4.x is untouched, never doubled).

/** Does the payload already carry any cachePoint block? */
export function hasCachePoint(payload) {
  const inSystem =
    Array.isArray(payload?.system) && payload.system.some((b) => b && b.cachePoint);
  const inMessages =
    Array.isArray(payload?.messages) &&
    payload.messages.some(
      (m) => Array.isArray(m?.content) && m.content.some((c) => c && c.cachePoint),
    );
  return Boolean(inSystem || inMessages);
}

/**
 * Mutate a Bedrock ConverseStream payload in place, adding default (short) cache
 * points on the system prompt and the last user message when none are present.
 * Mirrors the built-in placement: system prefix + conversation prefix.
 * @param {any} payload  ConverseStream command input ({ system, messages, ... }).
 * @returns {boolean} true if cache points were added.
 */
export function injectCachePoints(payload) {
  if (!payload || typeof payload !== "object") return false;
  if (hasCachePoint(payload)) return false;

  let added = false;
  if (Array.isArray(payload.system) && payload.system.length > 0) {
    payload.system.push({ cachePoint: { type: "default" } });
    added = true;
  }
  if (Array.isArray(payload.messages) && payload.messages.length > 0) {
    const last = payload.messages[payload.messages.length - 1];
    if (last && last.role === "user" && Array.isArray(last.content)) {
      last.content.push({ cachePoint: { type: "default" } });
      added = true;
    }
  }
  return added;
}

/**
 * The built-in adaptive-thinking id list misses Claude Haiku 5.x, so it sends
 * `thinking: {type: "enabled", budget_tokens}`, which Haiku 5.5 rejects. Rewrite
 * that to adaptive thinking with an effort level, as for the other Claude 5 models.
 * @param {any} payload  ConverseStream command input.
 * @param {string} effort  low | medium | high
 * @returns {boolean} true if the payload was rewritten.
 */
export function fixHaikuThinking(payload, effort) {
  const f = payload?.additionalModelRequestFields;
  if (!String(payload?.modelId).includes("haiku-5") || f?.thinking?.type !== "enabled") return false;
  const { budget_tokens: _b, type: _t, ...rest } = f.thinking;
  f.thinking = { ...rest, type: "adaptive" };
  f.output_config = { effort };
  delete f.anthropic_beta; // interleaved thinking is built in with adaptive
  return true;
}

/** Sonnet 5.5 and Haiku 5.5 return 400 on a non-default temperature; the built-in only strips it for Opus 5.5. */
export function stripTemperature(payload) {
  if (!/(sonnet|haiku)-5-5/.test(String(payload?.modelId)) || payload.inferenceConfig?.temperature === undefined) return false;
  delete payload.inferenceConfig.temperature;
  return true;
}

// --- self-check: `node inject.mjs` ---
if (import.meta.url === `file://${process.argv[1]}`) {
  const assert = (c, m) => { if (!c) { throw new Error("FAIL: " + m); } };

  // Claude 5 style: no cache -> both points added.
  const p5 = { system: [{ text: "sys" }], messages: [{ role: "user", content: [{ text: "hi" }] }] };
  assert(injectCachePoints(p5) === true, "5.x should add points");
  assert(p5.system.length === 2 && p5.system[1].cachePoint.type === "default", "5.x system cachePoint");
  assert(p5.messages[0].content.length === 2 && p5.messages[0].content[1].cachePoint, "5.x user cachePoint");

  // Claude 4.x style: built-in already cached -> untouched (no doubling).
  const p4 = {
    system: [{ text: "sys" }, { cachePoint: { type: "default" } }],
    messages: [{ role: "user", content: [{ text: "hi" }, { cachePoint: { type: "default" } }] }],
  };
  assert(injectCachePoints(p4) === false, "4.x should be left alone");
  assert(p4.system.length === 2, "4.x system unchanged");

  // Last message is assistant -> only system cache point, not conversation.
  const pa = { system: [{ text: "s" }], messages: [{ role: "assistant", content: [{ text: "ok" }] }] };
  assert(injectCachePoints(pa) === true, "assistant-last still caches system");
  assert(pa.messages[0].content.length === 1, "no cachePoint on assistant message");

  // Haiku 5.5 with budget thinking -> adaptive + effort; other models untouched.
  const ph = {
    modelId: "us.anthropic.claude-haiku-5-5",
    additionalModelRequestFields: {
      thinking: { type: "enabled", budget_tokens: 8192, display: "summarized" },
      anthropic_beta: ["interleaved-thinking-2025-05-14"],
    },
  };
  assert(fixHaikuThinking(ph, "medium") === true, "haiku-5 should be rewritten");
  const fh = ph.additionalModelRequestFields;
  assert(fh.thinking.type === "adaptive" && fh.thinking.display === "summarized", "haiku-5 adaptive");
  assert(!("budget_tokens" in fh.thinking) && !fh.anthropic_beta, "haiku-5 budget/beta removed");
  assert(fh.output_config.effort === "medium", "haiku-5 effort");
  const p45 = { modelId: "us.anthropic.claude-haiku-4-5-20251001-v1:0", additionalModelRequestFields: { thinking: { type: "enabled" } } };
  assert(fixHaikuThinking(p45, "low") === false, "haiku-4-5 left alone");
  assert(fixHaikuThinking({ modelId: "us.anthropic.claude-haiku-5-5" }, "low") === false, "no thinking -> no-op");

  const pt = { modelId: "us.anthropic.claude-sonnet-5-5", inferenceConfig: { maxTokens: 10, temperature: 0.5 } };
  assert(stripTemperature(pt) === true && !("temperature" in pt.inferenceConfig), "sonnet-5-5 temperature stripped");
  const pt5 = { modelId: "us.anthropic.claude-sonnet-5", inferenceConfig: { temperature: 0.5 } };
  assert(stripTemperature(pt5) === false && pt5.inferenceConfig.temperature === 0.5, "sonnet-5 temperature kept");

  console.log("inject.mjs self-check OK");
}
