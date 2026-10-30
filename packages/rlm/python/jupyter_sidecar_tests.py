import importlib.metadata
import json
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
SIDECAR = ROOT / "sidecar.py"
sys.path.insert(0, str(ROOT))
import sidecar


class ActualKernelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        case = unittest.TestCase()
        case.assertEqual(importlib.metadata.version("jupyter_client"), "8.6.3")
        case.assertEqual(importlib.metadata.version("ipykernel"), "6.30.1")
        case.assertEqual(importlib.metadata.version("numpy"), "2.3.4")
        with case.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("pydantic_monty")

        cls.temp = tempfile.TemporaryDirectory(prefix="pi-jupyter-sidecar-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.state = cls.root / "state.bin"
        cls.sentinels = {
            cls.state: b"state sentinel",
            cls.state.with_suffix(".names.json"): b"names sentinel",
            cls.state.with_suffix(".tmp"): b"temporary sentinel",
            cls.state.with_suffix(".corrupt"): b"corrupt sentinel",
        }
        for path, value in cls.sentinels.items():
            path.write_bytes(value)
            path.chmod(0)
        cls.proc = subprocess.Popen(
            [sys.executable, str(SIDECAR), "--state", str(cls.state)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if cls.proc.stdin is None or cls.proc.stdout is None or cls.proc.stderr is None:
            cls.proc.kill()
            raise AssertionError("sidecar pipes were not created")
        cls.stdin = cls.proc.stdin
        cls.stdout = cls.proc.stdout
        cls.stderr = cls.proc.stderr
        cls.addClassCleanup(cls._cleanup_process)
        ready = cls._read_line(10)
        case.assertTrue(ready.pop("generation"))
        case.assertEqual(ready, {"ready": True})
        cls.next_id = 1
        cls.kernel_pid = None

    @classmethod
    def _cleanup_process(cls):
        if cls.proc.poll() is not None:
            return
        try:
            cls.stdin.write(json.dumps({"id": "class-cleanup", "operation": "shutdown"}) + "\n")
            cls.stdin.flush()
            cls.proc.wait(timeout=5)
        except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
            cls.proc.kill()
            cls.proc.wait(timeout=2)

    @classmethod
    def tearDownClass(cls):
        try:
            if cls.proc.poll() is None:
                reply = cls._request("shutdown")
                if reply != {"id": reply["id"], "ok": True, "result": {"shutdown": True}}:
                    raise AssertionError(f"unexpected shutdown reply: {reply!r}")
                cls.stdin.close()
            cls.proc.wait(timeout=5)
            if cls.proc.returncode != 0:
                raise AssertionError(f"sidecar exit {cls.proc.returncode}: {cls.stderr.read()}")
            cls.stdout.close()
            cls.stderr.close()
            if cls.kernel_pid is not None:
                deadline = time.monotonic() + 5
                while Path(f"/proc/{cls.kernel_pid}").exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if Path(f"/proc/{cls.kernel_pid}").exists():
                    raise AssertionError("kernel PID survived shutdown")
            for path, value in cls.sentinels.items():
                path.chmod(0o600)
                if path.read_bytes() != value:
                    raise AssertionError(f"Monty state sentinel changed: {path}")
            if cls.state.with_suffix(".names.tmp").exists():
                raise AssertionError("Monty state artifact was created")
        finally:
            if cls.proc.poll() is None:
                cls.proc.kill()
                cls.proc.wait(timeout=2)
            cls.temp.cleanup()

    @classmethod
    def _read_line(cls, timeout):
        ready, _, _ = select.select([cls.stdout], [], [], timeout)
        if not ready:
            raise AssertionError("sidecar response timed out")
        line = cls.stdout.readline()
        if not line:
            raise AssertionError(f"sidecar exited early: {cls.stderr.read()}")
        return json.loads(line)

    @classmethod
    def _request(cls, operation="execute", code=""):
        request_id = cls.next_id
        cls.next_id += 1
        cls.stdin.write(json.dumps({"id": request_id, "operation": operation, "code": code}) + "\n")
        cls.stdin.flush()
        reply = cls._read_line(10)
        if reply.get("id") != request_id:
            raise AssertionError(f"response id {reply.get('id')!r} != request id {request_id!r}")
        return reply

    def test_01_provenance_numpy_and_retained_objects(self):
        reply = self._request(code="import os, sys, numpy as np\nkernel_pid = os.getpid()\na = np.arange(4)\n(sys.executable, kernel_pid)")
        self.assertTrue(reply["ok"], reply)
        executable, self.__class__.kernel_pid = reply["result"]["result"]
        self.assertEqual(executable, sys.executable)
        retained = self._request(code="int(a.sum())")
        self.assertEqual(retained["result"]["result"], 6)

    def test_02_stdout_stderr_expression_and_display_data(self):
        reply = self._request(code="import sys\nprint('out')\nprint('err', file=sys.stderr)\n42")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["output"], "out\n")
        self.assertEqual(reply["result"]["stderr"], "err\n")
        self.assertEqual(reply["result"]["result"], 42)
        displayed = self._request(code="from IPython.display import display\ndisplay('shown')")
        self.assertEqual(displayed["result"]["result"], "shown")

    def test_03_non_json_literals_fall_back_to_text_plain(self):
        for code, expected in (("{1, 2}", "{1, 2}"), ("b'abc'", "b'abc'"), ("1 + 2j", "(1+2j)")):
            with self.subTest(code=code):
                reply = self._request(code=code)
                self.assertTrue(reply["ok"], reply)
                self.assertEqual(reply["result"]["result"], expected)

    def test_04_rich_mime_is_explicitly_unsupported(self):
        reply = self._request(code="from IPython.display import display\ndisplay({'text/plain': 'fallback', 'text/html': '<b>x</b>'}, raw=True)")
        self.assertFalse(reply["ok"])
        self.assertIn("unsupported rich MIME", reply["error"])

    def test_05_input_fails_without_hanging(self):
        reply = self._request(code="input('prompt: ')")
        self.assertFalse(reply["ok"])
        self.assertIn("StdinNotImplementedError", reply["error"])

    def test_06_fields_are_bounded_and_error_keeps_mutation(self):
        streams = self._request(code="import sys\nprint('o' * 10000)\nprint('e' * 10000, file=sys.stderr)\n'r' * 10000")
        self.assertTrue(streams["result"]["truncated"])
        for field in ("output", "stderr"):
            self.assertLessEqual(len(streams["result"][field].encode()), 4096)
        result_text = streams["result"]["result"]
        self.assertLessEqual(len((result_text if isinstance(result_text, str) else json.dumps(result_text)).encode()), 4096)

        failed = self._request(code="kept = []\nkept.append(7)\nraise RuntimeError('x' * 20000)")
        self.assertFalse(failed["ok"])
        self.assertIn("RuntimeError", failed["error"])
        self.assertLessEqual(len(failed["error"].encode()), 8192)
        retained = self._request(code="kept")
        self.assertEqual(retained["result"]["result"], [7])


class BridgeStateMachineTests(unittest.TestCase):
    sidecar = sidecar
    emitted = []
    registry = sidecar.CellRegistry("uninitialized", emitted.append)

    def setUp(self):
        self.emitted = []
        self.registry = sidecar.CellRegistry("generation-a", self.emitted.append)

    def call(self, cell_id, call_id=1, **overrides):
        message = {
            "generation": "generation-a", "cell_id": cell_id, "call_id": call_id,
            "name": "read", "args": {"path": "a"},
        }
        message.update(overrides)
        return message

    def test_unmapped_call_waits_for_single_mapping_then_dispatches(self):
        cell_id = self.registry.allocate("request")
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(self.registry.dispatch(self.call(cell_id))))
        thread.start()
        time.sleep(0.02)
        self.assertEqual(self.emitted, [])
        self.registry.map(cell_id, "jupyter-message")
        deadline = time.monotonic() + 1
        while not self.emitted and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(self.emitted[0]["id"], "request")
        self.registry.settle({"type": "tool_result", "generation": "generation-a", "cell_id": cell_id, "call_id": 1, "result": {"ok": True}})
        thread.join(1)
        self.assertEqual(outcome[0]["result"], {"ok": True})
        with self.assertRaisesRegex(RuntimeError, "exactly once"):
            self.registry.map(cell_id, "other")

    def test_wrong_identity_never_dispatches(self):
        cell_id = self.registry.allocate("request")
        self.registry.map(cell_id, "jupyter-message")
        for message in (self.call(cell_id, generation="old"), self.call(cell_id + 1)):
            with self.assertRaisesRegex(RuntimeError, "expired"):
                self.registry.dispatch(message)
        self.assertEqual(self.emitted, [])

    def test_revoke_wakes_waiter_and_discards_late_result(self):
        cell_id = self.registry.allocate("request")
        self.registry.map(cell_id, "jupyter-message")
        errors = []
        thread = threading.Thread(target=lambda: self._capture_error(errors, lambda: self.registry.dispatch(self.call(cell_id))))
        thread.start()
        deadline = time.monotonic() + 1
        while not self.emitted and time.monotonic() < deadline:
            time.sleep(0.005)
        self.registry.revoke(cell_id)
        thread.join(1)
        self.assertRegex(str(errors[0]), "expired")
        self.registry.settle({"generation": "generation-a", "cell_id": cell_id, "call_id": 1, "result": "late"})
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.registry.dispatch(self.call(cell_id, call_id=2))
        self.assertEqual(len(self.emitted), 1)
        self.assertNotIn(cell_id, self.registry.cells)
        self.assertNotIn("request", self.registry.requests)

    def test_revoked_registry_entries_do_not_grow(self):
        for request_id in range(10_000):
            cell_id = self.registry.allocate(request_id)
            self.registry.map(cell_id, f"message-{request_id}")
            self.registry.revoke(cell_id)
        self.assertEqual(self.registry.cells, {})
        self.assertEqual(self.registry.requests, {})

    def test_bridge_close_wakes_and_joins_accept_thread(self):
        server = self.sidecar.BridgeServer("generation-a", lambda _message: None)
        thread = server.thread
        self.assertTrue(thread.is_alive())
        server.close()
        self.assertFalse(thread.is_alive())
        server.close()

    def test_duplicate_unknown_malformed_oversize_and_flood_revoke(self):
        invalid = [
            self.call(1, call_id=2),
            self.call(1, args=[]),
            self.call(1, args={"x": "m" * (self.sidecar.MAX_PAYLOAD_BYTES + 1)}),
            self.call(1, call_id=self.sidecar.MAX_CALLS_PER_CELL + 1),
        ]
        for message in invalid:
            with self.subTest(message={k: type(v).__name__ for k, v in message.items()}):
                registry = self.sidecar.CellRegistry("generation-a", lambda _message: None)
                cell_id = registry.allocate("request")
                registry.map(cell_id, "jupyter-message")
                message["cell_id"] = cell_id
                with self.assertRaises(RuntimeError):
                    registry.dispatch(message)
                self.assertNotIn(cell_id, registry.cells)
                self.assertNotIn("request", registry.requests)

    def test_serialized_frame_exact_boundaries_and_escaping(self):
        base = {"value": ""}
        overhead = len(self.sidecar.encode_frame(base)) - 1
        for size, accepted in ((self.sidecar.MAX_FRAME_BYTES - 1, True), (self.sidecar.MAX_FRAME_BYTES, True), (self.sidecar.MAX_FRAME_BYTES + 1, False)):
            message = {"value": "x" * (size - overhead)}
            if accepted:
                self.assertEqual(len(self.sidecar.encode_frame(message)) - 1, size)
            else:
                with self.assertRaisesRegex(ValueError, "serialized frame"):
                    self.sidecar.encode_frame(message)
        escaped = self.sidecar.encode_frame({"value": "\\\"\n"})
        multibyte = self.sidecar.encode_frame({"value": "\N{SNOWMAN}"})
        self.assertEqual(escaped, b'{"value":"\\\\\\\"\\n"}\n')
        self.assertIn(b"\\u2603", multibyte)

    def test_generation_and_ids_do_not_reuse(self):
        first = self.registry.allocate("first")
        second = self.registry.allocate("second")
        other = self.sidecar.CellRegistry("generation-b", lambda _message: None)
        self.assertEqual((first, second), (1, 2))
        self.assertNotEqual(self.registry.generation, other.generation)
        self.assertEqual(other.allocate("third"), 1)

    @staticmethod
    def _capture_error(target, action):
        try:
            action()
        except Exception as exc:
            target.append(exc)


class FakeChannel:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.reads = 0

    def get_msg(self, timeout=None):
        self.reads += 1
        if not self.messages:
            raise AssertionError("channel read past completion boundary")
        return self.messages.popleft()


class FakeClient:
    execute_kwargs = None

    def __init__(self, shell, iopub):
        self.shell_channel = FakeChannel(shell)
        self.iopub_channel = FakeChannel(iopub)
        self.started = False
        self.stopped = False
        self.ready_timeout = None

    def start_channels(self):
        self.started = True

    def stop_channels(self):
        self.stopped = True

    def wait_for_ready(self, timeout=None):
        self.ready_timeout = timeout
        return None

    def execute(self, code, **kwargs):
        self.execute_kwargs = kwargs
        return "wanted"


class FakeManager:
    def __init__(self, shell, iopub):
        self.client_instance = FakeClient(shell, iopub)
        self.started = False
        self.interrupted = False
        self.shutdown = False
        self.shutdown_calls = []
        self.shutdown_error: BaseException | None = None
        self.shutdown_wait_time = None

    def start_kernel(self):
        self.started = True

    def blocking_client(self):
        return self.client_instance

    def cleanup_resources(self):
        pass

    def interrupt_kernel(self):
        self.interrupted = True

    def shutdown_kernel(self, now=False):
        self.shutdown = True
        self.shutdown_calls.append(now)
        if self.shutdown_error:
            raise self.shutdown_error


class StartupCleanupTests(unittest.TestCase):
    def test_actual_kernel_readiness_failure_cleans_up_sidecar(self):
        class FakeProcess:
            def __init__(self):
                self.stdin = self
                self.stdout = self
                self.stderr = self
                self.returncode = None

            def poll(self):
                return self.returncode

            def read(self):
                return ""

            def write(self, value):
                return len(value)

            def flush(self):
                return None

            def wait(self, timeout=None):
                self.returncode = 0

        class FailingActualKernelTests(ActualKernelTests):
            @classmethod
            def _read_line(cls, timeout):
                raise AssertionError("readiness failed")

        proc = FakeProcess()
        try:
            with mock.patch.object(subprocess, "Popen", return_value=proc):
                with self.assertRaisesRegex(AssertionError, "readiness failed"):
                    FailingActualKernelTests.setUpClass()
            root = FailingActualKernelTests.root
        finally:
            FailingActualKernelTests.doClassCleanups()
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(root.exists())

    def test_partial_startup_failures_clean_up_started_resources(self):
        cases = ("start_kernel", "blocking_client", "start_channels", "wait_for_ready")
        for failure in cases:
            with self.subTest(failure=failure):
                manager = FakeManager([], [])
                if failure == "start_kernel":
                    def fail_start_kernel(message=failure, manager=manager):
                        manager.started = True
                        raise RuntimeError(message)
                    manager.start_kernel = fail_start_kernel
                elif failure == "blocking_client":
                    def fail_blocking_client(message=failure):
                        raise RuntimeError(message)
                    manager.blocking_client = fail_blocking_client
                elif failure == "start_channels":
                    def fail_start_channels(message=failure, manager=manager):
                        manager.client_instance.started = True
                        raise RuntimeError(message)
                    manager.client_instance.start_channels = fail_start_channels
                else:
                    def fail_wait_for_ready(timeout=None, message=failure):
                        raise RuntimeError(message)
                    manager.client_instance.wait_for_ready = fail_wait_for_ready

                with self.assertRaisesRegex(RuntimeError, failure) as caught:
                    sidecar.Kernel(Path("ignored"), manager_factory=lambda manager=manager: manager)
                self.assertTrue(manager.shutdown)
                self.assertEqual(manager.client_instance.stopped, failure in ("start_channels", "wait_for_ready"))
                if failure == "start_kernel":
                    self.assertRegex(str(caught.exception), r"kernel startup.*ipykernel.*locked.*dependencies")

    def test_cleanup_failures_are_notes_on_primary_startup_error(self):
        manager = FakeManager([], [])
        manager.client_instance.wait_for_ready = lambda timeout=None: (_ for _ in ()).throw(RuntimeError("primary"))
        manager.client_instance.stop_channels = lambda: (_ for _ in ()).throw(RuntimeError("stop failed"))
        manager.shutdown_kernel = lambda now=False: (_ for _ in ()).throw(RuntimeError("shutdown failed"))

        with self.assertRaisesRegex(RuntimeError, "primary") as caught:
            sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager)
        self.assertEqual(
            caught.exception.__notes__,
            ["channel cleanup failed: stop failed", "kernel cleanup failed: shutdown failed"],
        )

    def test_reset_shuts_down_old_kernel_when_channel_cleanup_fails(self):
        manager = FakeManager([], [])

        kernel = sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager)

        def fail_stop_channels():
            raise RuntimeError("stop failed")

        manager.client_instance.stop_channels = fail_stop_channels
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            kernel.reset()
        self.assertTrue(manager.shutdown)
        self.assertTrue(kernel.closed)
        self.assertFalse(kernel.bridge.thread.is_alive())


class KernelRevocationTests(unittest.TestCase):
    def make_kernel(self):
        managers = []

        def factory():
            manager = FakeManager([], [])
            managers.append(manager)
            return manager

        return sidecar.Kernel(Path("ignored"), manager_factory=factory), managers

    def waiting_call(self, kernel, request_id):
        registry = kernel.bridge.registry
        cell_id = registry.allocate(request_id)
        registry.map(cell_id, "jupyter-message")
        emitted = []
        errors = []
        registry.emit = emitted.append
        thread = threading.Thread(
            target=lambda: BridgeStateMachineTests._capture_error(
                errors,
                lambda: registry.dispatch({
                    "generation": registry.generation,
                    "cell_id": cell_id,
                    "call_id": 1,
                    "name": "read",
                    "args": {"path": "waiting"},
                }),
            )
        )
        thread.start()
        deadline = time.monotonic() + 1
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(len(emitted), 1)
        return registry, cell_id, thread, errors

    def assert_revoked_late_result_is_discarded(self, registry, cell_id, thread, errors):
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertRegex(str(errors[0]), "pi tool context expired")
        registry.settle({
            "type": "tool_result",
            "generation": registry.generation,
            "cell_id": cell_id,
            "call_id": 1,
            "result": "late",
        })
        self.assertNotIn(cell_id, registry.cells)
        self.assertNotIn("request", registry.requests)
        later_cell = registry.allocate("later")
        registry.map(later_cell, "later-message")
        self.assertEqual(registry.cells[later_cell]["calls"], {})

    def test_abort_revokes_waiting_tool_call_and_discards_late_result(self):
        kernel, managers = self.make_kernel()
        try:
            registry, cell_id, thread, errors = self.waiting_call(kernel, "request")
            kernel.abort("request")
            self.assertTrue(managers[0].interrupted)
            self.assert_revoked_late_result_is_discarded(registry, cell_id, thread, errors)
        finally:
            kernel.close()

    def test_reset_revokes_waiting_tool_call_and_discards_late_result(self):
        kernel, managers = self.make_kernel()
        try:
            registry, cell_id, thread, errors = self.waiting_call(kernel, "request")
            old_bridge_thread = kernel.bridge.thread
            old_generation = kernel.generation
            self.assertEqual(kernel.reset(), {
                "reset": True,
                "state_lost": True,
                "notice": "Python workspace reset; previous live state was discarded",
            })
            self.assertTrue(managers[0].shutdown)
            self.assertFalse(old_bridge_thread.is_alive())
            self.assertNotEqual(kernel.generation, old_generation)
            self.assert_revoked_late_result_is_discarded(registry, cell_id, thread, errors)
        finally:
            kernel.close()

    def test_close_revokes_waiting_tool_call_and_discards_late_result(self):
        kernel, managers = self.make_kernel()
        registry, cell_id, thread, errors = self.waiting_call(kernel, "request")
        bridge_thread = kernel.bridge.thread
        kernel.close()
        self.assertTrue(managers[0].shutdown)
        self.assertFalse(bridge_thread.is_alive())
        self.assert_revoked_late_result_is_discarded(registry, cell_id, thread, errors)


class ProtocolCorrelationTests(unittest.TestCase):
    @staticmethod
    def message(message_type, parent, **content):
        return {"header": {"msg_type": message_type}, "parent_header": {"msg_id": parent}, "content": content}

    def make_kernel(self, shell, iopub):
        manager = FakeManager(shell, iopub)
        return sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager), manager

    def test_ignores_unrelated_messages_and_requires_matching_reply_and_idle(self):
        m = self.message
        shell = [
            m("execute_reply", "wrong", status="ok"),
            m("execute_reply", "wanted", status="ok"),
            m("execute_reply", "after", status="ok"),
        ]
        iopub = [
            m("stream", "wrong", name="stdout", text="wrong stream"),
            m("execute_result", "wrong", data={"text/plain": "999"}),
            m("error", "wrong", ename="WrongError", evalue="wrong", traceback=["wrong traceback"]),
            m("status", "wrong", execution_state="idle"),
            m("stream", "wanted", name="stdout", text="right\n"),
            m("execute_result", "wanted", data={"text/plain": "7"}),
            m("status", "wanted", execution_state="idle"),
            m("stream", "after", name="stdout", text="past boundary"),
        ]
        kernel, manager = self.make_kernel(shell, iopub)
        try:
            result = kernel.execute(1, "code", [])
            self.assertEqual(result["output"], "right\n")
            self.assertEqual(result["result"], 7)
            self.assertEqual(manager.client_instance.shell_channel.reads, 2)
            self.assertEqual(manager.client_instance.iopub_channel.reads, 7)
            execute_kwargs = manager.client_instance.execute_kwargs
            if execute_kwargs is None:
                self.fail("execute kwargs were not recorded")
            self.assertFalse(execute_kwargs["allow_stdin"])
        finally:
            kernel.close()
        self.assertTrue(manager.shutdown)
        self.assertTrue(manager.client_instance.stopped)

    def test_synchronous_execute_submission_failure_revokes_allocated_cell(self):
        kernel, manager = self.make_kernel([], [])
        manager.client_instance.execute = lambda code, **kwargs: (_ for _ in ()).throw(RuntimeError("submit failed"))
        registry = kernel.bridge.registry
        try:
            with self.assertRaisesRegex(RuntimeError, "submit failed"):
                kernel.execute("request", "code", [])
            self.assertEqual(registry.cells, {})
            self.assertEqual(registry.requests, {})
        finally:
            kernel.close()

    def test_matching_error_is_reported_after_both_completion_boundaries(self):
        m = self.message
        shell = [m("execute_reply", "wanted", status="error")]
        iopub = [
            m("error", "wanted", ename="ValueError", evalue="bad", traceback=["trace line"]),
            m("status", "wanted", execution_state="idle"),
            m("status", "wanted", execution_state="idle"),
        ]
        kernel, manager = self.make_kernel(shell, iopub)
        try:
            with self.assertRaisesRegex(RuntimeError, r"(?s)ValueError.*bad.*trace line"):
                kernel.execute(1, "code", [])
            self.assertEqual(manager.client_instance.shell_channel.reads, 1)
            self.assertEqual(manager.client_instance.iopub_channel.reads, 2)
        finally:
            kernel.close()

    def test_many_large_chunks_stay_bounded_while_draining_to_idle(self):
        m = self.message
        shell = [m("execute_reply", "wanted", status="ok")]
        chunk = "x" * 5000 + "\N{SNOWMAN}" * 2000
        iopub = []
        for _ in range(100):
            iopub.extend([
                m("stream", "wanted", name="stdout", text=chunk),
                m("stream", "wanted", name="stderr", text=chunk),
                m("display_data", "wanted", data={"text/plain": repr(chunk)}),
                m("error", "wrong", ename="Wrong", evalue=chunk, traceback=[chunk]),
            ])
        iopub.extend([
            m("error", "wanted", ename="ValueError", evalue=chunk, traceback=[chunk] * 100),
            m("status", "wanted", execution_state="idle"),
        ])
        retained = []
        original = sidecar.BoundedAccumulator

        class ObservedAccumulator(original):
            def _record(self):
                retained.append((self.limit, self.retained_bytes))

            def append(self, text):
                super().append(text)
                self._record()

            def replace(self, text):
                super().replace(text)
                self._record()

        kernel, manager = self.make_kernel(shell, iopub)
        try:
            with mock.patch.object(sidecar, "BoundedAccumulator", ObservedAccumulator):
                with self.assertRaisesRegex(RuntimeError, "output truncated") as caught:
                    kernel.execute(1, "code", [])
            self.assertEqual(manager.client_instance.iopub_channel.reads, len(iopub))
            self.assertLessEqual(len(str(caught.exception).encode("utf-8")), 8192)
            self.assertTrue(retained)
            self.assertTrue(all(size <= limit for limit, size in retained), retained[-5:])
        finally:
            kernel.close()


if __name__ == "__main__":
    unittest.main()
