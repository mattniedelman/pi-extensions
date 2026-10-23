import { highlightCode, keyHint, type ToolDefinition, type Theme } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth, wrapTextWithAnsi, type Component } from "@earendil-works/pi-tui";

// Tool text is untrusted terminal input; retain line breaks, not escape controls.
function safeText(text: string): string {
  return text.replace(/\t/g, "    ").replace(/\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g, "")
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, "")
    .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, "");
}

function pretty(text: string): string {
  try { JSON.parse(text); } catch { return text; }
  // Format tokens, not JS numbers: Python integers can exceed Number precision.
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[^\s{}\[\],:]+|[{}\[\],:]/g) ?? [];
  let output = "";
  let depth = 0;
  const newline = () => "\n" + "  ".repeat(depth);
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === "{" || token === "[") {
      output += token;
      depth++;
      if (tokens[i + 1] !== "}" && tokens[i + 1] !== "]") output += newline();
    } else if (token === "}" || token === "]") {
      depth--;
      if (tokens[i - 1] !== "{" && tokens[i - 1] !== "[") output += newline();
      output += token;
    } else if (token === ",") output += token + newline();
    else if (token === ":") output += ": ";
    else output += token;
  }
  return output;
}

function component(lines: () => string[], limit: number, theme: Theme, clip = false): Component {
  return {
    invalidate() {},
    render(width: number) {
      if (width < 1) return [];
      const source = lines();
      const rendered = source.flatMap((line) => clip ? [truncateToWidth(line, width)] : wrapTextWithAnsi(line, width))
        .map((line) => truncateToWidth(line, width));
      const hidden = rendered.length > limit || (clip && source.some((line) => visibleWidth(line) > width));
      const shown = rendered.slice(0, limit);
      if (hidden) shown.push(truncateToWidth(theme.fg("muted", "... ") + keyHint("app.tools.expand", "to expand"), width));
      return shown;
    },
  };
}

export const renderPythonCall: NonNullable<ToolDefinition["renderCall"]> = (args, theme, context) => {
  const operation = safeText(String(args.operation ?? "execute"));
  const code = safeText(typeof args.code === "string" ? args.code : "").replace(/\r\n/g, "\n").trimEnd();
  return component(() => {
    const lines = code ? code.split("\n") : [];
    const count = lines.length ? ` | ${lines.length} line${lines.length === 1 ? "" : "s"}` : "";
    const title = theme.fg("toolTitle", theme.bold("Python")) + theme.fg("muted", ` | ${operation}${count}`);
    return [title, ...(code ? highlightCode(code, "python") : [])];
  }, context.expanded ? Infinity : 4, theme, !context.expanded);
};

export const renderPythonResult: NonNullable<ToolDefinition["renderResult"]> = (result, { expanded, isPartial }, theme, context) => {
  // Parse model-facing content, not details: result hooks may redact the former.
  const text = result.content.flatMap((block) => block.type === "text" ? [block.text] : []).join("\n");
  let data: Record<string, unknown> | undefined;
  try { data = JSON.parse(text); } catch { /* Errors/legacy results may be plain text. */ }
  const envelope = data && typeof data === "object" && !Array.isArray(data);
  return component(() => {
    if (isPartial) return [theme.fg("muted", "Running Python...")];
    if (context.isError) return [theme.fg("error", safeText(text || "Python execution failed"))];
    const lines: string[] = [];
    const section = (label: string, value: string) => {
      lines.push(theme.fg("muted", label), ...safeText(value).trimEnd().split("\n").map((line) => theme.fg("toolOutput", line)));
    };
    if (!envelope) {
      section("Output", text || "No output");
    } else if ("output" in data || "result" in data) {
      // Keep the warning first so even collapsed output cannot hide it.
      if (data.truncated) lines.push(theme.fg("warning", "Output or result truncated by the kernel"));
      if (typeof data.output === "string" && data.output.length) section("Output", pretty(data.output));
      if (data.result !== null && data.result !== undefined) {
        section("Result", typeof data.result === "string" ? data.result : JSON.stringify(data.result, null, 2));
      }
      if (!lines.length) lines.push(theme.fg("muted", "No output"));
    } else if (Array.isArray(data.names)) {
      section("Variables", data.names.length ? data.names.map(String).join(", ") : "No tracked variables");
    } else if (data.checkpointed === true) {
      lines.push(theme.fg("success", "Checkpoint saved"));
    } else if (data.reset === true) {
      lines.push(theme.fg("success", "Python state reset"));
    } else {
      section("Result", JSON.stringify(data, null, 2));
    }
    return lines;
  }, expanded ? Infinity : 8, theme);
};
