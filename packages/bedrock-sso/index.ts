import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import {
  type Api,
  type AssistantMessageEventStream,
  type Context,
  createAssistantMessageEventStream,
  type Model,
  type SimpleStreamOptions,
  streamSimple,
} from "@earendil-works/pi-ai";
import { getModels } from "@earendil-works/pi-ai/compat";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { fixHaikuThinking, injectCachePoints, stripTemperature } from "./inject.mjs";

// bedrock-sso: Claude on Amazon Bedrock, handing off to prime-agent's built-in
// bedrock-converse-stream code, plus two fixes:
//  1. Prompt caching for every Claude model. The bundled cache check skips
//     Claude 5 (prime-agent issue #2548), so cachePoint blocks are added here.
//  2. When the SSO session has fully expired, run `aws sso login` and retry once.
// Credentials come from the normal AWS SDK chain (AWS_PROFILE), which already
// refreshes SSO access tokens silently while the session is valid.

const PROVIDER = "bedrock-sso";
const API = "bedrock-sso-converse";
const LOGIN_TIMEOUT_MS = 180_000;
const SSO_ERROR = /aws sso login|sso.{0,40}(expired|invalid|refresh)|token.{0,20}expired|ExpiredToken/i;

const EFFORT = { off: null, minimal: null, low: "low", medium: "medium", high: "high", xhigh: "xhigh", max: "max" };
const US_EAST_1 = "https://bedrock-runtime.us-east-1.amazonaws.com";
// Released before prime-agent's catalog lists them (catalog entries win once added).
// Prices: Anthropic list price x1.1 for US inference profiles. Haiku 5.5 uses the
// <=100k-token prompt tier; longer prompts are billed 5x and under-reported here.
const EXTRA_MODELS = [
  {
    id: "us.anthropic.claude-sonnet-5-5",
    name: "Claude Sonnet 5.5 (US)",
    cost: { input: 2.2, output: 11, cacheRead: 0.11, cacheWrite: 2.75 },
  },
  {
    id: "us.anthropic.claude-haiku-5-5",
    name: "Claude Haiku 5.5 (US)",
    cost: { input: 0.11, output: 0.55, cacheRead: 0.011, cacheWrite: 0.1375 },
  },
].map((m) => ({
  ...m,
  api: "bedrock-converse-stream",
  provider: "amazon-bedrock",
  baseUrl: US_EAST_1,
  reasoning: true,
  thinkingLevelMap: EFFORT,
  input: ["text", "image"],
  contextWindow: 1_000_000,
  maxTokens: 128_000,
})) as unknown as Model<Api>[];

// ponytail: reads prime-agent's cached remote catalog (no extension API exposes it).
// Falls back to the small built-in stub; switch to an API if one appears.
function claudeModels(): Model<Api>[] {
  let models: Model<Api>[] = [];
  try {
    const file = join(homedir(), ".prime/agent/models/provider-model-catalog.v1.json");
    models = JSON.parse(readFileSync(file, "utf8")).payload.models;
  } catch {}
  if (!models?.length) models = getModels("amazon-bedrock") as Model<Api>[];
  const ids = new Set(models.map((m) => m.id));
  return [...models, ...EXTRA_MODELS.filter((m) => !ids.has(m.id))].filter(
    (m) => m.provider === "amazon-bedrock" && m.id.includes("anthropic.claude"),
  );
}

export default function (pi: ExtensionAPI) {
  let ui: any;
  let loggingIn: Promise<unknown> | null = null;
  pi.on("session_start", (_event, ctx) => {
    ui = ctx.ui;
  });

  const ssoLogin = () =>
    (loggingIn ??= (async () => {
      ui?.notify?.("AWS SSO session expired -- running 'aws sso login'. Approve it in your browser.", "warning");
      await pi.exec("aws", ["sso", "login"], { timeout: LOGIN_TIMEOUT_MS });
    })().finally(() => (loggingIn = null)));

  function stream(model: Model<Api>, context: Context, options?: SimpleStreamOptions): AssistantMessageEventStream {
    const out = createAssistantMessageEventStream();
    const run = () =>
      streamSimple({ ...model, api: "bedrock-converse-stream" } as Model<Api>, context, {
        ...options,
        onPayload: async (payload, m) => {
          injectCachePoints(payload);
          stripTemperature(payload);
          const level = options?.reasoning;
          if (level) fixHaikuThinking(payload, model.thinkingLevelMap?.[level] ?? (level === "minimal" ? "low" : level));
          return (await options?.onPayload?.(payload, m)) ?? payload;
        },
      });

    (async () => {
      for (let attempt = 0; attempt < 2; attempt++) {
        let forwarded = false;
        let retry = false;
        for await (const event of run()) {
          // Auth failures surface as an error before any output: log in, retry once.
          const msg = event.type === "error" ? (event.error.errorMessage ?? "") : "";
          if (attempt === 0 && !forwarded && SSO_ERROR.test(msg)) {
            retry = true;
            break;
          }
          forwarded = true;
          out.push(event);
        }
        if (!retry) break;
        await ssoLogin().catch(() => {}); // on failure the retry reports the original error
      }
      out.end();
    })();
    return out;
  }

  pi.registerProvider(PROVIDER, {
    name: "Amazon Bedrock (SSO)",
    baseUrl: "https://bedrock-runtime.us-east-1.amazonaws.com",
    apiKey: "aws-sdk-credential-chain", // unused: Bedrock signs with SigV4 from AWS_PROFILE
    api: API,
    models: claudeModels().map(({ provider: _p, api: _a, ...m }) => m as any),
    streamSimple: stream,
  });
}
