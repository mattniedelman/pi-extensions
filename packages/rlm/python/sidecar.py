import argparse
import ast
import asyncio
import base64
import json
import keyword
import queue
import secrets
import socket
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from jupyter_client.manager import KernelManager
except ModuleNotFoundError as exc:
    if exc.name != "jupyter_client":
        raise
    raise RuntimeError(
        "jupyter_client is unavailable; prepare the locked Jupyter dependencies before starting the sidecar"
    ) from exc

MAX_FRAME_BYTES = 1024 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
MAX_CALLS_PER_CELL = 64
EXPIRED_CONTEXT = "pi tool context expired"
INTERRUPT_IDLE_TIMEOUT = 2.0
STARTUP_DEADLINE = 10.0
OPERATION_DEADLINE = 10.0
SHUTDOWN_DEADLINE = 9.0
STATE_LOST_NOTICE = "Python workspace was lost after the interrupted kernel did not become idle"
RESET_NOTICE = "Python workspace reset; previous live state was discarded"
VARIABLES_PREFIX = "**PI_VARIABLES**"
VARIABLES_MIME = "application/vnd.pi.variables+json"
VARIABLES_MAX_NAMES = 256
VARIABLES_MAX_BYTES = 16384


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


class BoundedAccumulator:
    marker = b"\n... output truncated ...\n"

    def __init__(self, limit: int):
        self.limit = limit
        self.head_room = (limit - len(self.marker)) // 2
        self.tail_room = limit - len(self.marker) - self.head_room
        self.head = b""
        self.tail = b""
        self.truncated = False

    @property
    def retained_bytes(self) -> int:
        return len(self.head) + len(self.tail)

    def append(self, text: str) -> None:
        data = text.encode("utf-8")
        if not self.truncated and self.retained_bytes + len(data) <= self.limit:
            self.head += data
            return
        if not self.truncated:
            current = self.head
            self.head = (current + data[:self.head_room])[:self.head_room]
            needed = self.tail_room - min(len(data), self.tail_room)
            self.tail = current[-needed:] + data[-self.tail_room:] if needed else data[-self.tail_room:]
            self.truncated = True
        elif len(data) >= self.tail_room:
            self.tail = data[-self.tail_room:]
        else:
            self.tail = (self.tail + data)[-self.tail_room:]

    def replace(self, text: str) -> None:
        self.head = b""
        self.tail = b""
        self.truncated = False
        self.append(text)

    def value(self) -> tuple[str, bool]:
        data = self.head + (self.marker + self.tail if self.truncated else b"")
        text = data.decode("utf-8", errors="replace")
        while len(text.encode("utf-8")) > self.limit:
            text = text[:-1]
        return text, self.truncated


def encode_frame(message: dict[str, Any]) -> bytes:
    frame = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError(f"serialized frame exceeds {MAX_FRAME_BYTES} bytes")
    return frame + b"\n"


def raw_payload_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def bounded_call(label: str, function: Callable[[], Any], timeout: float) -> Any:
    if timeout <= 0:
        if label == "Jupyter channel cleanup":
            raise TimeoutError(f"Jupyter channel cleanup exceeded its {SHUTDOWN_DEADLINE:g}-second deadline")
        raise TimeoutError(f"{label} exceeded its deadline")
    result: list[Any] = []
    failure: list[BaseException] = []
    finished = threading.Event()

    def invoke() -> None:
        try:
            result.append(function())
        except BaseException as exc:
            failure.append(exc)
        finally:
            finished.set()

    threading.Thread(target=invoke, daemon=True).start()
    if not finished.wait(timeout):
        raise TimeoutError(f"{label} exceeded its {timeout:g}-second deadline")
    if failure:
        raise failure[0]
    return result[0] if result else None


def message_type(message: dict[str, Any]) -> str | None:
    return message.get("msg_type") or message.get("header", {}).get("msg_type")


def parent_id(message: dict[str, Any]) -> Any:
    return message.get("parent_header", {}).get("msg_id")


def text_value(text: str) -> Any:
    try:
        value = ast.literal_eval(text)
        json.dumps(value, allow_nan=False)
        return value
    except (SyntaxError, TypeError, ValueError):
        return text


class CellRegistry:
    def __init__(self, generation: str, emit: Callable[[dict[str, Any]], None]):
        self.generation = generation
        self.emit = emit
        self.condition = threading.Condition()
        self.next_cell = 1
        self.cells: dict[int, dict[str, Any]] = {}
        self.requests: dict[Any, int] = {}

    def allocate(self, request_id: Any) -> int:
        with self.condition:
            cell_id = self.next_cell
            self.next_cell += 1
            self.cells[cell_id] = {"request_id": request_id, "msg_id": None, "state": "allocated-unmapped", "next_call": 1, "calls": {}}
            self.requests[request_id] = cell_id
            return cell_id

    def map(self, cell_id: int, msg_id: str) -> bool:
        with self.condition:
            cell = self.cells.get(cell_id)
            if cell is None:
                return False
            if cell["state"] == "aborted-unmapped":
                cell["msg_id"] = msg_id
                cell["state"] = "aborted-active"
                self.condition.notify_all()
                return True
            if cell["state"] != "allocated-unmapped" or cell["msg_id"] is not None:
                raise RuntimeError("cell may be mapped exactly once")
            cell["msg_id"] = msg_id
            cell["state"] = "mapped-active"
            self.condition.notify_all()
            return True

    def dispatch(self, message: dict[str, Any]) -> dict[str, Any]:
        generation = message.get("generation")
        cell_id = message.get("cell_id")
        call_id = message.get("call_id")
        with self.condition:
            if generation != self.generation or not isinstance(cell_id, int):
                raise RuntimeError(EXPIRED_CONTEXT)
            cell = self.cells.get(cell_id)
            if cell is None:
                raise RuntimeError(EXPIRED_CONTEXT)
            while cell["state"] == "allocated-unmapped":
                self.condition.wait()
            if cell["state"] == "aborted-active":
                self._revoke_locked(cell_id)
                raise RuntimeError(EXPIRED_CONTEXT)
            if cell["state"] != "mapped-active":
                raise RuntimeError(EXPIRED_CONTEXT)
            if not isinstance(call_id, int) or call_id != cell["next_call"] or call_id > MAX_CALLS_PER_CELL:
                self._revoke_locked(cell_id)
                raise RuntimeError("invalid or excessive pi tool call")
            if not isinstance(message.get("name"), str) or not isinstance(message.get("args"), dict):
                self._revoke_locked(cell_id)
                raise RuntimeError("malformed pi tool call")
            if raw_payload_size(message["args"]) > MAX_PAYLOAD_BYTES:
                self._revoke_locked(cell_id)
                raise RuntimeError("pi tool arguments exceed limit")
            waiter = {"done": False, "result": None}
            cell["calls"][call_id] = waiter
            cell["next_call"] += 1
            outbound = {
                "id": cell["request_id"], "type": "tool_call", "generation": self.generation,
                "cell_id": cell_id, "call_id": call_id, "name": message["name"], "args": message["args"],
            }
            try:
                self.emit(outbound)
            except Exception:
                self._revoke_locked(cell_id)
                raise
            while not waiter["done"] and cell["state"] == "mapped-active":
                self.condition.wait()
            if cell["state"] != "mapped-active" or not waiter["done"]:
                raise RuntimeError(EXPIRED_CONTEXT)
            result = waiter["result"]
            del cell["calls"][call_id]
            if not isinstance(result, dict):
                raise RuntimeError("malformed pi tool result")
            return result

    def settle(self, message: dict[str, Any]) -> None:
        with self.condition:
            if message.get("generation") != self.generation:
                return
            cell_id = message.get("cell_id")
            call_id = message.get("call_id")
            if not isinstance(cell_id, int) or not isinstance(call_id, int):
                return
            cell = self.cells.get(cell_id)
            if not cell or cell["state"] != "mapped-active":
                return
            waiter = cell["calls"].get(call_id)
            if not waiter or waiter["done"]:
                self._revoke_locked(cell_id)
                return
            if "result" in message and raw_payload_size(message["result"]) > MAX_PAYLOAD_BYTES:
                self._revoke_locked(cell_id)
                return
            waiter["result"] = message
            waiter["done"] = True
            self.condition.notify_all()

    def revoke_request(self, request_id: Any) -> None:
        with self.condition:
            cell_id = self.requests.get(request_id)
            cell = self.cells.get(cell_id) if cell_id is not None else None
            if cell is not None and cell["state"] == "allocated-unmapped":
                cell["state"] = "aborted-unmapped"
                self.condition.notify_all()
                return
            if cell_id is not None:
                self._revoke_locked(cell_id)

    def revoke(self, cell_id: int) -> None:
        with self.condition:
            self._revoke_locked(cell_id)

    def _revoke_locked(self, cell_id: int) -> None:
        cell = self.cells.get(cell_id)
        if not cell:
            return
        cell["state"] = "revoking"
        self.condition.notify_all()
        cell["calls"].clear()
        cell["state"] = "revoked"
        self.condition.notify_all()
        self.cells.pop(cell_id, None)
        if self.requests.get(cell["request_id"]) == cell_id:
            self.requests.pop(cell["request_id"], None)


class BridgeServer:
    def __init__(self, generation: str, emit: Callable[[dict[str, Any]], None]):
        self.registry = CellRegistry(generation, emit)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen()
        self.closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def address(self) -> tuple[str, int]:
        return self.socket.getsockname()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            if self.closed:
                connection.close()
                return
            threading.Thread(target=self._connection, args=(connection,), daemon=True).start()

    def _connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                file = connection.makefile("rb")
                frame = file.readline(MAX_FRAME_BYTES + 2)
                if len(frame.rstrip(b"\n")) > MAX_FRAME_BYTES:
                    raise ValueError("pi bridge frame exceeds limit")
                if not frame.endswith(b"\n"):
                    raise ValueError("pi bridge requires one newline-delimited frame")
                message = json.loads(frame)
                reply = self.registry.dispatch(message)
                connection.sendall(encode_frame(reply))
            except Exception as exc:
                try:
                    connection.sendall(encode_frame({"error": str(exc)}))
                except (OSError, ValueError):
                    pass

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        with self.registry.condition:
            for cell_id in list(self.registry.cells):
                self.registry._revoke_locked(cell_id)
        try:
            with socket.create_connection(self.address):
                pass
        finally:
            self.socket.close()
        self.thread.join()


def proxy_source(host: str, port: int, generation: str, cell_id: int, tools: list[dict[str, Any]]) -> str:
    methods = []
    for tool in tools:
        method = tool.get("method")
        if method and isinstance(method, str) and method.isidentifier() and not keyword.iskeyword(method):
            methods.append(f"    def {method}(self, **kwargs): return self.call({tool['name']!r}, kwargs)")
    return "\n".join([
        "import json as _pi_json, socket as _pi_socket",
        f"_pi_identity = ({host!r}, {port}, {generation!r}, {cell_id})",
        "class __PiProxy:",
        "    def __init__(self, identity): self._identity, self._next_call = identity, 1",
        "    def call(self, name, args):",
        "        host, port, generation, cell_id = self._identity",
        "        call_id, self._next_call = self._next_call, self._next_call + 1",
        "        message = {'generation': generation, 'cell_id': cell_id, 'call_id': call_id, 'name': name, 'args': args}",
        "        raw = _pi_json.dumps(message, ensure_ascii=True, separators=(',', ':')).encode()",
        f"        if len(raw) > {MAX_FRAME_BYTES}: raise RuntimeError('pi bridge frame exceeds limit')",
        "        with _pi_socket.create_connection((host, port)) as connection:",
        "            connection.sendall(raw + b'\\n')",
        f"            response = connection.makefile('rb').readline({MAX_FRAME_BYTES + 2})",
        f"        if len(response.rstrip(b'\\n')) > {MAX_FRAME_BYTES}: raise RuntimeError('pi bridge frame exceeds limit')",
        "        answer = _pi_json.loads(response)",
        "        if 'error' in answer: raise RuntimeError(answer['error'])",
        "        if answer.get('error'): raise RuntimeError(answer['error'])",
        "        return answer.get('result', {})",
        "    def tools(self): return self.call('__bridge_tools__', {})",
        "    def search(self, query): return self.call('__bridge_search__', {'query': query})",
        "    def describe(self, name): return self.call('__bridge_describe__', {'name': name})",
        *methods,
        "pi = __PiProxy(_pi_identity)",
    ])


def close_jupyter_event_loop() -> None:
    from jupyter_core import utils as jupyter_utils

    loop = jupyter_utils._loop.get()
    if loop is not None and not loop.is_running():
        loop.close()
    jupyter_utils._loop.set(None)
    asyncio.set_event_loop(None)


def cleanup_manager(manager: Any) -> None:
    try:
        manager.cleanup_resources()
    finally:
        close_jupyter_event_loop()


class Kernel:
    def __init__(
        self,
        state_path: Path,
        manager_factory: Callable[[], Any] = KernelManager,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ):
        del state_path  # Accepted for compatibility with the sidecar CLI.
        self.manager_factory = manager_factory
        self.install_kernel_helpers = manager_factory is KernelManager
        self.emit = emit or (lambda _message: None)
        self.client: Any = None
        self.manager: Any = None
        self.closed = False
        self.terminal_cleanup_failure = False
        self._abort_condition = threading.Condition()
        self._aborted_requests: set[Any] = set()
        self._start()

    def _start(self, *, deadline: float | None = None) -> None:
        self.generation = secrets.token_hex(16)
        self.bridge = BridgeServer(self.generation, self.emit)
        self.manager = None
        kernel_start_attempted = False
        kernel_started = False
        channels_start_attempted = False
        start_deadline = deadline if deadline is not None else time.monotonic() + STARTUP_DEADLINE

        def call(label: str, function: Callable[[], Any]) -> Any:
            return bounded_call(label, function, start_deadline - time.monotonic())

        try:
            self.manager = call("Jupyter manager construction", self.manager_factory)
            kernel_start_attempted = True
            call("Jupyter kernel startup", self.manager.start_kernel)
            kernel_started = True
            self.client = call("Jupyter client construction", self.manager.blocking_client)
            channels_start_attempted = True
            call("Jupyter channel startup", self.client.start_channels)
            ready_timeout = start_deadline - time.monotonic()
            if ready_timeout <= 0:
                raise TimeoutError("kernel replacement exceeded retirement deadline")
            call("Jupyter kernel readiness", lambda: self.client.wait_for_ready(timeout=ready_timeout))
            self.variables_helper = f"__pi_variables_{secrets.token_hex(16)}"
            if self.install_kernel_helpers:
                self._install_variables_helper(start_deadline)
            close_jupyter_event_loop()
        except Exception as exc:
            cleanup_notes = []
            if channels_start_attempted and self.client is not None:
                try: call("Jupyter channel cleanup", self.client.stop_channels)
                except Exception as cleanup_exc: cleanup_notes.append(f"channel cleanup failed: {cleanup_exc}")
            if kernel_start_attempted and self.manager is not None:
                try: call("Jupyter kernel cleanup", lambda: self.manager.shutdown_kernel(now=True))
                except Exception as cleanup_exc: cleanup_notes.append(f"kernel cleanup failed: {cleanup_exc}")
            self.bridge.close()
            close_jupyter_event_loop()
            self.closed = True
            if not kernel_started:
                error = RuntimeError("kernel startup failed; verify ipykernel is available in the locked Jupyter dependencies: " f"{exc}")
            elif channels_start_attempted:
                error = RuntimeError("kernel readiness failed; verify ipykernel is available in the locked Jupyter dependencies: " f"{exc}")
            else:
                error = exc
            for note in cleanup_notes:
                error.add_note(note)
            if error is exc:
                raise
            raise error from exc

    def _install_variables_helper(self, deadline: float) -> None:
        source = "\n".join([
            "import base64 as __pi_b64, json as __pi_json",
            "def __pi_make_variables(__pi_ns, __pi_publish, __pi_dumps, __pi_encode, __pi_str, __pi_type, __pi_sorted, __pi_len):",
            "    def __pi_variables():",
            "        __pi_all = __pi_sorted(k for k in __pi_ns.keys() if __pi_type(k) is __pi_str and not k.startswith('_') and k != 'pi')",
            f"        __pi_names = __pi_all[:{VARIABLES_MAX_NAMES}]",
            f"        __pi_truncated = __pi_len(__pi_all) > {VARIABLES_MAX_NAMES}",
            "        while True:",
            "            __pi_raw = __pi_dumps({'names': __pi_names, 'truncated': __pi_truncated}, ensure_ascii=False, separators=(',', ':')).encode('utf-8')",
            f"            if __pi_len(__pi_raw) <= {VARIABLES_MAX_BYTES}: break",
            "            __pi_names.pop()",
            "            __pi_truncated = True",
            f"        __pi_publish(data={{{VARIABLES_MIME!r}: {VARIABLES_PREFIX!r} + __pi_encode(__pi_raw).decode('ascii')}}, metadata={{}})",
            "    return __pi_variables",
            f"{self.variables_helper} = __pi_make_variables(get_ipython().user_ns, get_ipython().display_pub.publish, __pi_json.dumps, __pi_b64.b64encode, str, type, sorted, len)",
            "del __pi_make_variables, __pi_b64, __pi_json",
        ])
        msg_id = bounded_call(
            "variables helper submission",
            lambda: self.client.execute(source, silent=True, allow_stdin=False, store_history=False, stop_on_error=True),
            deadline - time.monotonic(),
        )
        error = None
        idle = shell_reply = False
        while not shell_reply or not idle:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("variables helper installation exceeded its deadline")
            timeout = min(0.05, remaining)
            if not shell_reply:
                try:
                    message = self.client.shell_channel.get_msg(timeout=timeout)
                    if parent_id(message) == msg_id and message_type(message) == "execute_reply":
                        shell_reply = True
                        if message.get("content", {}).get("status") != "ok":
                            error = "variables helper installation failed"
                except queue.Empty:
                    pass
            if not idle:
                try:
                    message = self.client.iopub_channel.get_msg(timeout=timeout)
                except queue.Empty:
                    message = None
                if message is not None and parent_id(message) == msg_id:
                    content = message.get("content", {})
                    if message_type(message) == "error":
                        error = f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
                    elif message_type(message) == "status" and content.get("execution_state") == "idle":
                        idle = True
        if error:
            raise RuntimeError(error)

    def _was_aborted(self, request_id: Any) -> bool:
        with self._abort_condition:
            return request_id in self._aborted_requests

    def _consume_abort(self, request_id: Any) -> bool:
        with self._abort_condition:
            if request_id not in self._aborted_requests:
                return False
            self._aborted_requests.discard(request_id)
            return True

    def _retire_after_failed_interrupt(self) -> dict[str, Any]:
        old_bridge = self.bridge
        old_client = self.client
        self.closed = True
        old_bridge.close()
        self.manager.shutdown_wait_time = 9
        failure: list[BaseException] = []
        finished = threading.Event()

        def shutdown() -> None:
            try:
                self.manager.shutdown_kernel(now=False)
            except BaseException as exc:
                failure.append(exc)
            finally:
                close_jupyter_event_loop()
                finished.set()

        worker = threading.Thread(target=shutdown, daemon=True)
        worker.start()
        retirement_deadline = time.monotonic() + SHUTDOWN_DEADLINE
        completed = finished.wait(max(0.0, retirement_deadline - time.monotonic()))
        if completed:
            try:
                bounded_call(
                    "Jupyter channel cleanup",
                    old_client.stop_channels,
                    max(0.0, retirement_deadline - time.monotonic()),
                )
                bounded_call("Jupyter resource cleanup", lambda: cleanup_manager(self.manager), max(0.0, retirement_deadline - time.monotonic()))
            except BaseException as exc:
                failure.append(exc)
        else:
            failure.append(TimeoutError(f"shutdown_kernel(now=False) exceeded its {SHUTDOWN_DEADLINE:g}-second deadline"))
        if failure:
            self.terminal_cleanup_failure = True
            raise RuntimeError(f"kernel cleanup failed; explicit lifecycle restart required: {failure[0]}") from failure[0]
        self.closed = False
        try:
            self._start(deadline=retirement_deadline)
        except BaseException as exc:
            self.terminal_cleanup_failure = True
            raise RuntimeError(f"kernel replacement failed within retirement deadline: {exc}") from exc
        return {"aborted": True, "state_lost": True, "generation": self.generation, "notice": STATE_LOST_NOTICE}

    def execute(self, request_id: Any, code: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
        if self.terminal_cleanup_failure or self.closed:
            raise RuntimeError("kernel cleanup failed; explicit lifecycle restart required")
        if len(code.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"code exceeds {MAX_PAYLOAD_BYTES} bytes")
        with self._abort_condition:
            if request_id in self._aborted_requests:
                self._aborted_requests.discard(request_id)
                return {"aborted": True, "state_lost": False, "generation": self.generation}
        def was_aborted() -> bool:
            with self._abort_condition:
                return request_id in self._aborted_requests

        cell_id = self.bridge.registry.allocate(request_id)
        try:
            host, port = self.bridge.address
            source = proxy_source(host, port, self.generation, cell_id, tools) + "\n" + code
            msg_id = bounded_call(
                "Jupyter cell submission",
                lambda: self.client.execute(source, allow_stdin=False, store_history=True, stop_on_error=True),
                OPERATION_DEADLINE,
            )
            if not self.bridge.registry.map(cell_id, msg_id):
                return {"aborted": True, "state_lost": False, "generation": self.generation}
        except BaseException:
            self.bridge.registry.revoke(cell_id)
            raise
        try:
            stdout = BoundedAccumulator(4096)
            stderr = BoundedAccumulator(4096)
            result = BoundedAccumulator(4096)
            error = BoundedAccumulator(8192)
            has_result = has_error = idle = shell_reply = False
            deadline = None
            while not shell_reply or not idle:
                aborted = self._was_aborted(request_id)
                if aborted and deadline is None:
                    deadline = time.monotonic() + INTERRUPT_IDLE_TIMEOUT
                timeout = max(0.001, deadline - time.monotonic()) if deadline is not None else 0.05
                if not shell_reply:
                    try:
                        message = self.client.shell_channel.get_msg(timeout=timeout)
                        if parent_id(message) == msg_id and message_type(message) == "execute_reply":
                            shell_reply = True
                    except queue.Empty:
                        pass
                if not idle:
                    try:
                        message = self.client.iopub_channel.get_msg(timeout=timeout)
                    except queue.Empty:
                        message = None
                    if message is not None and parent_id(message) == msg_id:
                        kind = message_type(message)
                        content = message.get("content", {})
                        if kind == "stream":
                            (stderr if content.get("name") == "stderr" else stdout).append(str(content.get("text", "")))
                        elif kind in ("execute_result", "display_data"):
                            data = content.get("data", {})
                            rich_types = set(data) - {"text/plain"}
                            if rich_types or "text/plain" not in data:
                                error.replace("unsupported rich MIME result" + (f": {', '.join(sorted(rich_types))}" if rich_types else "")); has_error = True
                            else:
                                result.replace(json.dumps(text_value(str(data["text/plain"])), ensure_ascii=True)); has_result = True
                        elif kind == "error":
                            error.replace(f"{content.get('ename', 'Error')}: {content.get('evalue', '')}")
                            for line in content.get("traceback", []): error.append("\n" + str(line))
                            has_error = True
                        elif kind == "status" and content.get("execution_state") == "idle": idle = True
                if deadline is not None and time.monotonic() >= deadline:
                    return self._retire_after_failed_interrupt()
            if self._was_aborted(request_id):
                return {"aborted": True, "state_lost": False, "generation": self.generation}
            if has_error: raise RuntimeError(error.value()[0])
            output, output_truncated = stdout.value(); error_output, stderr_truncated = stderr.value(); encoded, result_truncated = result.value()
            value = encoded if result_truncated else json.loads(encoded) if has_result else None
            return {"result": value, "output": output, "stderr": error_output, "truncated": output_truncated or stderr_truncated or result_truncated}
        finally:
            self.bridge.registry.revoke(cell_id)
            with self._abort_condition: self._aborted_requests.discard(request_id)

    def variables(self, request_id: Any) -> dict[str, Any]:
        if self.terminal_cleanup_failure or self.closed:
            raise RuntimeError("kernel cleanup failed; explicit lifecycle restart required")
        if self._consume_abort(request_id):
            return {"aborted": True, "state_lost": False, "generation": self.generation}
        deadline = time.monotonic() + OPERATION_DEADLINE
        msg_id = bounded_call(
            "variables submission",
            lambda: self.client.execute(
                f"{self.variables_helper}()", silent=False, allow_stdin=False, store_history=False, stop_on_error=True
            ),
            OPERATION_DEADLINE,
        )
        encoded = None
        error = None
        idle = shell_reply = False
        while not shell_reply or not idle:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("variables operation exceeded its deadline")
            timeout = min(0.05, remaining)
            if not shell_reply:
                try:
                    message = self.client.shell_channel.get_msg(timeout=timeout)
                    if parent_id(message) == msg_id and message_type(message) == "execute_reply":
                        shell_reply = True
                except queue.Empty:
                    pass
            if not idle:
                try:
                    message = self.client.iopub_channel.get_msg(timeout=timeout)
                except queue.Empty:
                    message = None
                if message is not None and parent_id(message) == msg_id:
                    kind = message_type(message)
                    content = message.get("content", {})
                    if kind == "display_data":
                        data = content.get("data", {})
                        if set(data) == {VARIABLES_MIME} and type(data[VARIABLES_MIME]) is str:
                            encoded = data[VARIABLES_MIME]
                    elif kind == "error":
                        error = f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
                    elif kind == "status" and content.get("execution_state") == "idle":
                        idle = True
        if error:
            raise RuntimeError(error)
        try:
            if not encoded or not encoded.startswith(VARIABLES_PREFIX):
                raise ValueError("missing sentinel")
            raw = base64.b64decode(encoded[len(VARIABLES_PREFIX):], validate=True)
            if len(raw) > VARIABLES_MAX_BYTES:
                raise ValueError("oversize envelope")
            result = json.loads(raw)
            if (
                not isinstance(result, dict)
                or set(result) != {"names", "truncated"}
                or not isinstance(result["names"], list)
                or not all(type(name) is str for name in result["names"])
                or result["names"] != sorted(set(result["names"]))
                or any(name.startswith("_") or name == "pi" for name in result["names"])
                or len(result["names"]) > VARIABLES_MAX_NAMES
                or type(result["truncated"]) is not bool
                or json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8") != raw
            ):
                raise ValueError("invalid envelope")
            return result
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("invalid variables protocol response") from exc

    def abort(self, request_id: Any) -> None:
        with self._abort_condition:
            self._aborted_requests.add(request_id)
            self._abort_condition.notify_all()
        self.bridge.registry.revoke_request(request_id)
        self.manager.interrupt_kernel()

    def reset(self) -> dict[str, Any]:
        if self.terminal_cleanup_failure or self.closed:
            raise RuntimeError("kernel cleanup failed; explicit lifecycle restart required")
        deadline = time.monotonic() + SHUTDOWN_DEADLINE
        self.closed = True
        self.bridge.close()
        try:
            failure: list[BaseException] = []
            try:
                bounded_call("Jupyter channel reset", self.client.stop_channels, max(0.0, deadline - time.monotonic()))
            except BaseException as exc:
                failure.append(exc)
            try:
                bounded_call("Jupyter kernel reset", lambda: self.manager.shutdown_kernel(now=False), max(0.0, deadline - time.monotonic()))
            except BaseException as exc:
                failure.append(exc)
            try:
                bounded_call("Jupyter resource reset", lambda: cleanup_manager(self.manager), max(0.0, deadline - time.monotonic()))
            except BaseException as exc:
                failure.append(exc)
            if failure:
                raise RuntimeError(str(failure[0])) from failure[0]
        except BaseException:
            self.terminal_cleanup_failure = True
            raise
        self.closed = False
        try:
            self._start()
        except BaseException:
            self.terminal_cleanup_failure = True
            self.closed = True
            raise
        return {"reset": True, "state_lost": True, "notice": RESET_NOTICE}

    def close(self) -> None:
        if self.closed: return
        self.closed = True
        self.bridge.close()
        try:
            deadline = time.monotonic() + SHUTDOWN_DEADLINE
            if self.client is not None:
                bounded_call("Jupyter channel close", self.client.stop_channels, max(0.0, deadline - time.monotonic()))
            if self.manager is not None:
                bounded_call("Jupyter kernel close", lambda: self.manager.shutdown_kernel(now=False), max(0.0, deadline - time.monotonic()))
                bounded_call("Jupyter resource close", lambda: cleanup_manager(self.manager), max(0.0, deadline - time.monotonic()))
        except BaseException:
            self.terminal_cleanup_failure = True


class InputReader(threading.Thread):
    def __init__(self, requests: queue.Queue[dict[str, Any] | None], get_kernel: Callable[[], Kernel | None]):
        super().__init__(daemon=True); self.requests = requests; self.get_kernel = get_kernel

    def run(self) -> None:
        while True:
            frame = sys.stdin.buffer.readline(MAX_FRAME_BYTES + 2)
            if not frame: self.requests.put(None); return
            if len(frame.rstrip(b"\n")) > MAX_FRAME_BYTES:
                self.requests.put({"_input_error": f"input frame exceeds {MAX_FRAME_BYTES} bytes"})
                self.requests.put(None)
                return
            try:
                message = json.loads(frame)
                if not isinstance(message, dict):
                    raise ValueError("top-level JSON must be an object")
            except Exception as exc:
                self.requests.put({"_input_error": f"invalid JSON: {exc}"})
                continue
            if message.get("type") == "tool_result":
                kernel = self.get_kernel()
                if kernel: kernel.bridge.registry.settle(message)
            elif message.get("operation") == "abort":
                kernel = self.get_kernel()
                if kernel:
                    try:
                        kernel.abort(message.get("id"))
                    except Exception as exc:
                        self.requests.put({"id": message.get("id"), "_input_error": f"abort failed: {exc}"})
            else: self.requests.put(message)


def dispatch_operation(kernel: Kernel, request_id: Any, operation: str, code: str, tools: list[dict[str, Any]]) -> dict[str, Any]:
    if operation == "execute":
        return kernel.execute(request_id, code, tools)
    if operation == "variables":
        return kernel.variables(request_id)
    if operation == "reset":
        return kernel.reset()
    if operation == "checkpoint":
        raise ValueError("Python checkpoints are unsupported; state lasts only for the live pi session.")
    raise ValueError(f"unsupported operation: {operation}")


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path)
    return parser


def main() -> None:
    argument_parser().parse_args()
    output_lock = threading.Lock()
    def emit(message: dict[str, Any]) -> None:
        frame = encode_frame(message)
        with output_lock: sys.stdout.buffer.write(frame); sys.stdout.buffer.flush()
    requests: queue.Queue[dict[str, Any] | None] = queue.Queue()
    kernel = Kernel(Path("ignored"), emit=emit)
    holder = {"kernel": kernel}
    reader = InputReader(requests, lambda: holder.get("kernel")); reader.start()
    emit({"ready": True, "generation": kernel.generation})
    try:
        while True:
            request = requests.get()
            if request is None: break
            request_id = request.get("id") if isinstance(request, dict) else None
            try:
                if "_input_error" in request: raise ValueError(request["_input_error"])
                operation = request.get("operation", "execute")
                if operation == "shutdown":
                    emit({"id": request_id, "ok": True, "result": {"shutdown": True}})
                    break
                result = dispatch_operation(
                    kernel,
                    request_id,
                    operation,
                    str(request.get("code", "")),
                    request.get("tools", []),
                )
                emit({"id": request_id, "ok": True, "result": result})
            except Exception as exc:
                error, _ = bounded(f"{type(exc).__name__}: {exc}"); emit({"id": request_id, "ok": False, "error": error})
    finally:
        kernel.close()


if __name__ == "__main__": main()
