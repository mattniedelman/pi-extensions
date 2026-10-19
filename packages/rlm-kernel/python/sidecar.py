import argparse
import ast
import json
import keyword
import os
import sys
from pathlib import Path
from typing import Any

from pydantic_monty import CollectStreams, FunctionSnapshot, Monty, MontyComplete, NameLookupSnapshot, ResourceLimits

LIMITS = ResourceLimits(
    max_feed_duration_secs=2.0,
    max_memory=int(os.environ.get("PI_RLM_MAX_MEMORY", 256 * 1024 * 1024)),
)


def bounded(text: str, limit: int = 8192) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text, False
    marker = b"\n... output truncated ...\n"
    room = limit - len(marker)
    head = data[: room // 2]
    tail = data[-(room - len(head)) :]
    clipped = (head + marker + tail).decode("utf-8", errors="replace")
    while len(clipped.encode("utf-8")) > limit:
        clipped = clipped[:-1]
    return clipped, True


def assigned_names(code: str) -> set[str]:
    try:
        body = ast.parse(code).body
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in body:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(child.id for child in ast.walk(target) if isinstance(child, ast.Name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return names


def safe_value(value: Any) -> Any:
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return repr(value)


def proxy_source(tools: list[dict[str, Any]]) -> str:
    lines = [
        "class Pi:",
        "    def call(self, name, args):",
        "        return __pi_call__(name, args)",
        "    def tools(self):",
        "        return __pi_call__('__bridge_tools__', {})",
        "    def search(self, query):",
        "        return __pi_call__('__bridge_search__', {'query': query})",
        "    def describe(self, name):",
        "        return __pi_call__('__bridge_describe__', {'name': name})",
    ]
    for tool in tools:
        method = tool.get("method")
        if method and method.isidentifier() and not keyword.iskeyword(method):
            lines.extend([
                f"    def {method}(self, **kwargs):",
                f"        return __pi_call__({tool['name']!r}, kwargs)",
            ])
    lines.append("pi = Pi()")
    return "\n".join(lines)


def read_message() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line:
        raise EOFError("pi tool bridge closed")
    return json.loads(line)



class Kernel:
    def __init__(self, state_path: Path):
        self.state_path = state_path
        self.pool = Monty()
        self.pool.__enter__()
        self.session = None
        self.names_path = state_path.with_suffix(".names.json")
        try:
            self.names = set(json.loads(self.names_path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError):
            self.names: set[str] = set()
        self._replace(self._read_state())

    def _read_state(self) -> bytes | None:
        try:
            return self.state_path.read_bytes()
        except FileNotFoundError:
            return None

    def _replace(self, state: bytes | None) -> None:
        if self.session is not None:
            self.session.__exit__(None, None, None)
        self.session = self.pool.checkout(limits=LIMITS)
        self.session.__enter__()
        if state:
            try:
                self.session.load_session(state)
            except Exception:
                self.state_path.rename(self.state_path.with_suffix(".corrupt"))
                self.session.__exit__(None, None, None)
                self.session = self.pool.checkout(limits=LIMITS)
                self.session.__enter__()
                self.names.clear()

    def checkpoint(self) -> None:
        blob = self.session.dump()
        self.state_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.state_path.parent, 0o700)
        temp = self.state_path.with_suffix(".tmp")
        temp.write_bytes(blob)
        os.chmod(temp, 0o600)
        os.replace(temp, self.state_path)
        names_temp = self.names_path.with_suffix(".tmp")
        names_temp.write_text(json.dumps(sorted(self.names)))
        os.chmod(names_temp, 0o600)
        os.replace(names_temp, self.names_path)

    def execute(self, request_id: Any, code: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        previous = self.session.dump()
        previous_names = self.names.copy()
        streams = CollectStreams()
        try:
            source = proxy_source(tools) + "\n" + code
            state = self.session.feed_start(source, print_callback=streams)
            while not isinstance(state, MontyComplete):
                if isinstance(state, NameLookupSnapshot):
                    state = state.resume()
                    continue
                if not isinstance(state, FunctionSnapshot) or state.function_name != "__pi_call__":
                    raise RuntimeError(f"unsupported Monty suspension: {type(state).__name__}")
                name, args = state.args
                print(json.dumps({
                    "id": request_id,
                    "type": "tool_call",
                    "call_id": state.call_id,
                    "name": name,
                    "args": args,
                }, ensure_ascii=True), flush=True)
                response = read_message()
                if response.get("type") != "tool_result" or response.get("call_id") != state.call_id:
                    raise RuntimeError("invalid pi tool response")
                if response.get("error"):
                    answer = {"exc_type": "RuntimeError", "message": str(response["error"])}
                else:
                    result = response.get("result", {})
                    answer = {"return_value": result}
                state = state.resume(answer)
            result = state.output
            self.names.update(assigned_names(code))
            self.checkpoint()
        except Exception:
            self.names = previous_names
            self._replace(previous)
            raise
        printed = "".join(text for _stream, text in streams.output)
        text, output_truncated = bounded(printed, 4096)
        value = safe_value(result)
        encoded = json.dumps(value, ensure_ascii=True)
        encoded, result_truncated = bounded(encoded, 4096)
        return {
            "result": encoded if result_truncated else value,
            "output": text,
            "truncated": output_truncated or result_truncated,
        }

    def variables(self) -> dict[str, Any]:
        return {"names": sorted(name for name in self.names if not name.startswith("_"))}

    def reset(self) -> dict[str, Any]:
        self._replace(None)
        self.names.clear()
        self.state_path.unlink(missing_ok=True)
        self.names_path.unlink(missing_ok=True)
        return {"reset": True}

    def close(self) -> None:
        if self.session is not None:
            self.session.__exit__(None, None, None)
            self.session = None
        self.pool.__exit__(None, None, None)


def respond(request_id: Any, *, result: Any = None, error: str | None = None) -> None:
    message = {"id": request_id, "ok": error is None}
    if error is None:
        message["result"] = result
    else:
        message["error"] = error
    print(json.dumps(message, ensure_ascii=True), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    kernel = Kernel(args.state)
    print(json.dumps({"ready": True}), flush=True)
    try:
        for line in sys.stdin:
            request: dict[str, Any] = {}
            try:
                request = json.loads(line)
                operation = request.get("operation", "execute")
                if operation == "execute":
                    result = kernel.execute(request.get("id"), str(request.get("code", "")), request.get("tools", []))
                elif operation == "variables":
                    result = kernel.variables()
                elif operation == "reset":
                    result = kernel.reset()
                elif operation == "checkpoint":
                    kernel.checkpoint()
                    result = {"checkpointed": True}
                elif operation == "shutdown":
                    respond(request.get("id"), result={"shutdown": True})
                    break
                else:
                    raise ValueError(f"unknown operation: {operation}")
                respond(request.get("id"), result=result)
            except Exception as exc:
                error, _ = bounded(f"{type(exc).__name__}: {exc}")
                respond(request.get("id") if isinstance(request, dict) else None, error=error)
    finally:
        kernel.close()


if __name__ == "__main__":
    main()
