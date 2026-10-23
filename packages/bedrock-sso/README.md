# bedrock-sso

Registers a `bedrock-sso` provider with every Claude model from the Amazon Bedrock catalog.
Requests are handed to Prime Agent's built-in `bedrock-converse-stream` code, with these fixes:

1. **Prompt caching for all Claude models.** The bundled cache check in Prime Agent 0.9.6 only
   matches Claude 4.x / 3.7 / 3.5 ids, so Claude 5 models are billed full price every turn
   ([prime-agent#2548](https://github.com/PrimeIntellect-ai/prime-agent/issues/2548)).
   This adds `cachePoint` blocks on the system prompt and last user message when none are present.
2. **Claude Sonnet 5.5 and Haiku 5.5.** `us.anthropic.claude-sonnet-5-5` and
   `us.anthropic.claude-haiku-5-5` are added until Prime Agent's catalog lists them. Haiku 5.5 only
   accepts adaptive thinking, but the built-in code sends budget thinking for it, so the payload is
   rewritten to `thinking: adaptive` + `output_config.effort`. `temperature` is dropped for both
   (they reject non-default values).
3. **SSO re-login.** If a call fails with an SSO / expired-token error before any output,
   it runs `aws sso login` (using `AWS_PROFILE`) and retries once.

## Setup

- Set `AWS_PROFILE` to an SSO profile with Bedrock access. The AWS SDK refreshes SSO access
  tokens silently while the SSO session is valid.
- Select models as `bedrock-sso/<model-id>`, e.g. `bedrock-sso/us.anthropic.claude-opus-5-5`.

## Why a separate provider

Overriding the built-in `amazon-bedrock` provider from an extension has no effect in
Prime Agent 0.9.6: the model list and stream function are ignored.

## Limits

- The model list is read from Prime Agent's cached catalog
  (`~/.prime/agent/models/provider-model-catalog.v1.json`), falling back to the small built-in list.
- If `aws sso login` cannot open a browser, the login URL is not shown. Run it manually.

## Test

`node inject.mjs`
