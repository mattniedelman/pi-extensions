// No-model renderer checks using pi's actual theme/components and registered tool.
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import { getMarkdownTheme, initTheme } from "@earendil-works/pi-coding-agent";
import { visibleWidth } from "@earendil-works/pi-tui";
import factory from "./index.ts";

const plain = (lines) => lines.join("\n").replace(/\x1b\[[0-9;]*m/g, "");

export function renderingChecks(activeTheme?) {
  let tool;
  factory({ on() {}, registerTool(value) { tool = value; } });
  assert.equal(typeof tool.renderCall, "function", "custom call renderer missing");
  assert.equal(typeof tool.renderResult, "function", "custom result renderer missing");
  // These callbacks delegate to pi's current theme; no fixed test-only palette.
  const md = getMarkdownTheme();
  const theme = activeTheme ?? { fg: (_name, text) => md.codeBlock(text), bold: (text) => md.bold(text) };
  const context = (args = {}, expanded = false, isError = false) => ({ args, expanded, isError });
  const render = (data, expanded = false, isError = false, args = {}) => {
    const result = { content: [{ type: "text", text: JSON.stringify(data) }], details: data };
    const before = JSON.stringify(result);
    const component = tool.renderResult(result, { expanded, isPartial: false }, theme, context(args, expanded, isError));
    assert.equal(JSON.stringify(result), before, "renderer mutated model result");
    return component;
  };
  let passed = 0;
  const check = (action) => { action(); passed++; };
  const snapshots = {};
  check(() => {
    const code = "value = {\"new_external_calls\": 0, \"retained_characters\": 43835}\nprint(json.dumps(value))";
    const call = tool.renderCall({ code }, theme, context({ code }));
    const lines = call.render(80);
    const text = plain(lines);
    assert.match(text, /Python.*execute.*2 lines/);
    assert.ok(!text.includes("code:"));
    assert.ok(lines.join("").includes("\x1b["), "Python syntax highlighting missing");
    snapshots.call = lines;
  });
  check(() => {
    const code = "value = '" + "x".repeat(600) + "'\n" + "value += 'x'\n".repeat(12);
    const compact = tool.renderCall({ code }, theme, context({ code })).render(40);
    const expanded = tool.renderCall({ code }, theme, context({ code }, true)).render(40);
    assert.ok(compact.length <= 6, "unbounded compact code");
    assert.ok(plain(compact).includes("expand"));
    assert.ok(expanded.length > compact.length);
    for (const line of [...compact, ...expanded]) assert.ok(visibleWidth(line) <= 40);
  });
  check(() => {
    const data = { result: null, output: '{"new_external_calls":0,"retained_characters":43835}\n', truncated: false };
    const lines = render(data, true).render(80);
    const text = plain(lines);
    assert.match(text, /Output/);
    assert.match(text, /\n\s*"retained_characters": 43835/);
    assert.ok(!text.includes('\\"'));
    assert.ok(!text.includes('"result": null') && !text.includes('"truncated": false'));
    snapshots.json = lines;
  });
  check(() => {
    const text = plain(render({ result: null, output: "first\nsecond\n", truncated: false }).render(80));
    assert.match(text, /first\nsecond/);
    assert.ok(!text.includes('"output"'));
  });
  check(() => {
    const output = "{not valid JSON}\n";
    assert.ok(plain(render({ result: null, output }).render(80)).includes("{not valid JSON}"));
    assert.match(plain(render({ result: 0, output: "" }).render(80)), /0/);
    assert.match(plain(render({ result: false, output: "" }).render(80)), /false/);
    assert.match(plain(render({ result: null, output: "" }).render(80)), /No output/);
  });
  check(() => {
    const data = { result: null, output: "row\n".repeat(50), truncated: true };
    const compact = render(data).render(40);
    assert.ok(compact.length <= 12, "unbounded compact output");
    assert.match(plain(compact), /truncated/i);
    assert.match(plain(compact), /expand/);
    assert.ok(render(data, true).render(40).length > compact.length);
    snapshots.truncated = compact;
  });
  check(() => {
    const result = { content: [{ type: "text", text: "MontyRuntimeError: NameError: name 'missing' is not defined" }] };
    const lines = tool.renderResult(result, { expanded: false, isPartial: false }, theme, context({}, false, true)).render(80);
    assert.match(plain(lines), /NameError/);
    snapshots.error = lines;
    const redacted = { content: [{ type: "text", text: "Redacted by policy" }], details: { output: "DO_NOT_SHOW_STALE_DETAILS" } };
    const text = plain(tool.renderResult(redacted, { expanded: true, isPartial: false }, theme, context()).render(80));
    assert.ok(!text.includes("DO_NOT_SHOW_STALE_DETAILS"), "stale details leaked after content redaction");
    assert.match(text, /Redacted by policy/);
  });
  check(() => {
    assert.match(plain(render({ names: ["x", "data"] }, false, false, { operation: "variables" }).render(80)), /x.*data/);
    assert.match(plain(render({ checkpointed: true }, false, false, { operation: "checkpoint" }).render(80)), /checkpoint/i);
    assert.match(plain(render({ reset: true }, false, false, { operation: "reset" }).render(80)), /reset/i);
  });
  check(() => {
    const data = { result: null, output: "\u754c\ud83d\ude42e\u0301".repeat(40) + "\nnext\n", truncated: false };
    const component = render(data);
    for (const width of [1, 12, 40, 80, 120]) {
      const lines = component.render(width);
      for (const line of lines) assert.ok(visibleWidth(line) <= width, `overflow at ${width}`);
      component.invalidate();
    }
    const text = plain(render({ output: "\x1b[2J\x1b]52;c;payload\x07safe" }).render(80));
    assert.ok(!text.includes("\x1b"), "output injected terminal controls");
  });
  check(() => {
    const lines = tool.renderResult({ content: [] }, { expanded: false, isPartial: true }, theme, context()).render(80);
    assert.match(plain(lines), /Running/);
    const unsafe = { operation: "execute\x1b]52;c;payload\x07", code: "if True:\n\tprint('tab')" };
    const call = tool.renderCall(unsafe, theme, context(unsafe, true)).render(80);
    assert.ok(!plain(call).includes("\x1b") && !plain(call).includes("\t"));
    for (const output of ['{"n":9007199254740993}', '{"n":1e309}', '{"n":-0.0}', '{"text":"a, b: {c}","items":[{},[]]}']) {
      const text = plain(render({ output, result: null }, true).render(120));
      const tokens = output.match(/"(?:\\.|[^"\\])*"|[^\s{}\[\],:]+|[{}\[\],:]/g);
      const renderedTokens = text.replace(/^Output\n/, "").match(/"(?:\\.|[^"\\])*"|[^\s{}\[\],:]+|[{}\[\],:]/g);
      assert.deepEqual(renderedTokens, tokens, "JSON formatting changed token values");
    }
  });
  return { passed, snapshots };
}

export default function (host) {
  host.registerCommand("probe-rlm", {
    description: "Exercise actual RLM renderer without model inference",
    handler: async (_args, ctx) => {
      try {
        // Batched light/dark snapshots use the host theme, not a test palette.
        initTheme("dark");
        const dark = renderingChecks(ctx.ui.theme);
        initTheme("light");
        const light = renderingChecks(ctx.ui.theme);
        initTheme("dark");
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify({ ...dark, lightSnapshots: light.snapshots }));
      } catch (error) {
        writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify({ error: error.stack }));
      }
    },
  });
}
