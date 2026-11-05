import gc
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import jupyter_sidecar_tests as support
import sidecar


class InterruptLifecycleTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        gc.collect()

    def message(self, kind, parent="wanted", **content):
        return {"header": {"msg_type": kind}, "parent_header": {"msg_id": parent}, "content": content}

    def kernel(self, shell, iopub):
        manager = support.FakeManager(shell, iopub)
        kernel = sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager)
        return kernel, manager

    def test_matching_idle_after_interrupt_retains_generation_and_namespace(self):
        m = self.message
        kernel, manager = self.kernel(
            [m("execute_reply", status="abort")],
            [m("status", "other", execution_state="idle"), m("status", execution_state="idle")],
        )
        old_generation = kernel.generation
        old_bridge = kernel.bridge
        manager.client_instance.execute = lambda code, **kwargs: (kernel.abort("request"), "wanted")[1]
        try:
            result = kernel.execute("request", "while True: pass", [])
            self.assertEqual(result, {"aborted": True, "state_lost": False, "generation": old_generation})
            self.assertIs(kernel.bridge, old_bridge)
            self.assertTrue(manager.interrupted)
            self.assertFalse(manager.shutdown)
        finally:
            kernel.close()

    def test_abort_during_submit_never_dispatches_revoked_tool(self):
        emitted = []
        manager = support.FakeManager(
            [self.message("execute_reply", status="abort")],
            [self.message("status", execution_state="idle")],
        )
        kernel = sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager, emit=emitted.append)
        manager.client_instance.execute = lambda code, **kwargs: (kernel.abort("race"), "wanted")[1]
        try:
            result = kernel.execute("race", "while True: pass", [])
            self.assertTrue(result["aborted"])
            self.assertIsNone(kernel.bridge.registry.requests.get("race"))
            self.assertEqual(emitted, [])
        finally:
            kernel.close()

    def test_no_idle_uses_one_public_bounded_shutdown_and_rotates_generation(self):
        kernel, manager = self.kernel([], [])
        old_generation = kernel.generation
        manager.client_instance.execute = lambda code, **kwargs: (kernel.abort("request"), "wanted")[1]
        manager.client_instance.iopub_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        manager.client_instance.shell_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        try:
            with mock.patch.object(sidecar, "INTERRUPT_IDLE_TIMEOUT", 0.01):
                result = kernel.execute("request", "stubborn", [])
            self.assertTrue(result["aborted"])
            self.assertTrue(result["state_lost"])
            self.assertEqual(result["notice"], "Python workspace was lost after the interrupted kernel did not become idle")
            self.assertNotEqual(result["generation"], old_generation)
            self.assertEqual(manager.shutdown_calls, [False])
            self.assertEqual(manager.shutdown_wait_time, 9)
            self.assertTrue(manager.client_instance.stopped)
        finally:
            kernel.close()

    def test_retirement_deadline_covers_shutdown_and_replacement_startup(self):
        kernel, manager = self.kernel([], [])
        starts = iter([0.0, 0.02, 0.08] + [0.08] * 30)
        ready_deadlines = []
        original_start = kernel._start

        def replacement_start(*, deadline=None):
            ready_deadlines.append(deadline)
            return original_start(deadline=deadline)

        kernel._start = replacement_start
        try:
            with mock.patch.object(sidecar, "SHUTDOWN_DEADLINE", 0.1), mock.patch.object(sidecar.time, "monotonic", side_effect=lambda: next(starts)):
                kernel._retire_after_failed_interrupt()
            self.assertEqual(ready_deadlines, [0.1])
            ready_timeout = manager.client_instance.ready_timeout
            self.assertIsNotNone(ready_timeout)
            assert ready_timeout is not None
            self.assertLess(abs(ready_timeout - 0.02), 1e-9)
        finally:
            kernel.close()

    def test_shutdown_cleanup_failure_is_terminal_and_disables_restart(self):
        kernel, manager = self.kernel([], [])
        manager.client_instance.execute = lambda code, **kwargs: (kernel.abort("request"), "wanted")[1]
        manager.client_instance.iopub_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        manager.client_instance.shell_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        manager.shutdown_error = RuntimeError("cleanup exploded")
        with mock.patch.object(sidecar, "INTERRUPT_IDLE_TIMEOUT", 0.01):
            with self.assertRaisesRegex(RuntimeError, "kernel cleanup failed.*cleanup exploded"):
                kernel.execute("request", "stubborn", [])
        self.assertTrue(kernel.closed)
        self.assertTrue(kernel.terminal_cleanup_failure)
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            kernel.execute("later", "1", [])
        self.assertEqual(manager.shutdown_calls, [False])
        kernel.close()

    def test_shutdown_deadline_is_terminal(self):
        kernel, manager = self.kernel([], [])
        manager.client_instance.execute = lambda code, **kwargs: (kernel.abort("request"), "wanted")[1]
        manager.client_instance.iopub_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        manager.client_instance.shell_channel.get_msg = lambda timeout=None: (_ for _ in ()).throw(queue.Empty())
        blocker = threading.Event()
        with mock.patch.object(manager, "shutdown_kernel", side_effect=lambda now=False: blocker.wait(1)):
            try:
                with mock.patch.object(sidecar, "INTERRUPT_IDLE_TIMEOUT", 0.01), mock.patch.object(sidecar, "SHUTDOWN_DEADLINE", 0.01):
                    with self.assertRaisesRegex(RuntimeError, "cleanup failed.*0.01-second deadline"):
                        kernel.execute("request", "stubborn", [])
                self.assertTrue(kernel.terminal_cleanup_failure)
            finally:
                blocker.set()
                kernel.close()


class RealInterruptTests(unittest.TestCase):
    READY_TEXT = "QC6_SIGIGN_READY"

    @classmethod
    def setUpClass(cls):
        if not sys.platform.startswith("linux") or not Path("/proc/self/stat").exists():
            raise RuntimeError("qc6 cancellation fixtures require Linux /proc")
        if not hasattr(signal, "SIGINT") or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("qc6 cancellation fixtures require SIGINT and pidfd signal support")

    @staticmethod
    def process_identity(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except FileNotFoundError:
            return None

    def run_interrupt_fixture(self, code, marker, *, require_ready):
        kernel = sidecar.Kernel(Path("ignored"))
        old_pid = kernel.execute("identity", "import os\nos.getpid()", [])["result"]
        old_identity = self.process_identity(old_pid)
        old_generation = kernel.generation
        stream_ready = threading.Event()
        original_get_msg = kernel.client.iopub_channel.get_msg

        def observe_stream(timeout=None):
            message = original_get_msg(timeout=timeout)
            cell_id = kernel.bridge.registry.requests.get("stubborn")
            cell = kernel.bridge.registry.cells.get(cell_id) if cell_id is not None else None
            if (cell and sidecar.parent_id(message) == cell["msg_id"]
                    and sidecar.message_type(message) == "stream"
                    and self.READY_TEXT in str(message.get("content", {}).get("text", ""))):
                stream_ready.set()
            return message

        kernel.client.iopub_channel.get_msg = observe_stream
        outcome = []
        errors = []

        def execute():
            try:
                outcome.append(kernel.execute("stubborn", code, []))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute)
        thread.start()
        try:
            if require_ready:
                deadline = time.monotonic() + 5
                while (not marker.exists() or not stream_ready.is_set()) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(marker.exists(), "SIGINT-ignore marker was not created")
                self.assertTrue(stream_ready.is_set(), "matching SIGINT-ignore readiness stream was not observed")
            started = time.monotonic()
            kernel.abort("stubborn")
            thread.join(12)
            elapsed = time.monotonic() - started
            self.assertFalse(thread.is_alive(), "interrupt fixture exceeded its 12-second bound")
            self.assertEqual(errors, [])
            self.assertEqual(len(outcome), 1)
            return kernel, outcome[0], elapsed, old_pid, old_identity, old_generation
        except BaseException:
            kernel.close()
            raise

    def test_real_interrupt_retains_pid_generation_object_and_holds_next_cell(self):
        emitted = []
        kernel = sidecar.Kernel(Path("ignored"), emit=emitted.append)
        try:
            seeded = kernel.execute("seed", "import os\nkept = object()\n(os.getpid(), id(kept))", [])
            pid, object_id = seeded["result"]
            generation = kernel.generation
            outcome = []
            thread = threading.Thread(target=lambda: outcome.append(kernel.execute("loop", "while True:\n    pass", [])))
            thread.start()
            time.sleep(0.15)
            kernel.abort("loop")
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome, [{"aborted": True, "state_lost": False, "generation": generation}])
            retained = kernel.execute("after", "(os.getpid(), id(kept))", [])
            self.assertEqual(retained["result"], [pid, object_id])
        finally:
            kernel.close()

    def test_sigint_ignore_forces_bounded_retirement_without_touching_sentinel(self):
        sentinel = subprocess.Popen(
            [sys.executable, "-c", "import signal; print('ready', flush=True); signal.pause()"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        assert sentinel.stdout is not None
        self.assertEqual(sentinel.stdout.readline(), "ready\n")
        sentinel_identity = self.process_identity(sentinel.pid)
        root = Path(tempfile.mkdtemp(prefix="pi-qc6-sigint-ignore-"))
        marker = root / "ready"
        kernel = None
        try:
            code = (
                "import os, signal\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                f"_qc6_tmp = {str(marker.with_suffix('.tmp'))!r}\n"
                "with open(_qc6_tmp, 'x') as _qc6_file:\n"
                "    _qc6_file.write('ready')\n"
                f"os.replace(_qc6_tmp, {str(marker)!r})\n"
                f"print({self.READY_TEXT!r}, flush=True)\n"
                "await __import__('asyncio').sleep(0.3)\n"
                "while True:\n"
                "    pass"
            )
            kernel, result, elapsed, old_pid, old_identity, old_generation = self.run_interrupt_fixture(
                code, marker, require_ready=True,
            )
            self.assertEqual(result, {
                "aborted": True,
                "state_lost": True,
                "generation": kernel.generation,
                "notice": sidecar.STATE_LOST_NOTICE,
            })
            self.assertLessEqual(elapsed, 12)
            self.assertNotEqual(kernel.generation, old_generation)
            self.assertNotEqual(
                self.process_identity(old_pid), old_identity,
                "old kernel PID/starttime identity remained live after forced retirement",
            )
            self.assertIsNone(kernel.execute("fresh-namespace", "globals().get('_qc6_tmp')", [])["result"])
            self.assertIsNone(sentinel.poll(), "unrelated sentinel was signaled")
            self.assertEqual(self.process_identity(sentinel.pid), sentinel_identity)
        finally:
            if kernel is not None:
                kernel.close()
            marker.unlink(missing_ok=True)
            marker.with_suffix(".tmp").unlink(missing_ok=True)
            root.rmdir()
            self.assertFalse(root.exists(), "readiness root survived cleanup")
            self.assertIsNone(sentinel.poll(), "unrelated sentinel died before fixture cleanup")
            self.assertEqual(self.process_identity(sentinel.pid), sentinel_identity)
            sentinel.terminate()
            sentinel.wait(timeout=2)
            assert sentinel.stdout is not None
            sentinel.stdout.close()

    def test_missing_sigint_ignore_does_not_force_retirement(self):
        with tempfile.TemporaryDirectory(prefix="pi-qc6-control-") as directory:
            marker = Path(directory) / "ready"
            code = (
                "import os\n"
                f"open({str(marker)!r}, 'x').close()\n"
                f"print({self.READY_TEXT!r}, flush=True)\n"
                "await __import__('asyncio').sleep(0.3)\n"
                "while True:\n"
                "    pass"
            )
            kernel, result, _elapsed, _pid, _identity, generation = self.run_interrupt_fixture(code, marker, require_ready=True)
            try:
                self.assertEqual(result, {"aborted": True, "state_lost": False, "generation": generation})
            finally:
                kernel.close()

    def test_interrupt_before_readiness_does_not_force_retirement(self):
        with tempfile.TemporaryDirectory(prefix="pi-qc6-control-") as directory:
            marker = Path(directory) / "never-ready"
            pre_marker = Path(directory) / "started"
            code = (
                "import os, signal\n"
                f"open({str(pre_marker)!r}, 'x').close()\n"
                f"_qc6_gate = {str(Path(directory) / 'gate')!r}\n"
                "while not os.path.exists(_qc6_gate):\n"
                "    pass\n"
                "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
                f"open({str(marker)!r}, 'x').close()\n"
                f"print({self.READY_TEXT!r}, flush=True)"
            )
            kernel = sidecar.Kernel(Path("ignored"))
            generation = kernel.generation
            outcome = []
            thread = threading.Thread(target=lambda: outcome.append(kernel.execute("stubborn", code, [])))
            thread.start()
            try:
                deadline = time.monotonic() + 5
                while not pre_marker.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(pre_marker.exists(), "pre-readiness control did not start")
                kernel.abort("stubborn")
                thread.join(3)
                self.assertFalse(thread.is_alive(), "pre-readiness interrupt did not complete")
                self.assertFalse(marker.exists())
                self.assertEqual(outcome, [{"aborted": True, "state_lost": False, "generation": generation}])
            finally:
                kernel.close()


if __name__ == "__main__":
    unittest.main()
