import contextlib
import io
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sidecar

ROOT = Path(__file__).resolve().parent
BOUNDARIES = ("raw-write", "names-write", "raw-replace", "names-replace")
CHILD_TIMEOUT = 5
spec = importlib.util.spec_from_file_location("isolated_runner", ROOT.parent / "isolated-check.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def run_checks(command=None, *, timeout=20):
    command = command or [sys.executable, str(ROOT / "recovery_check.py")]
    status = runner.run_owned(command, cwd=ROOT, env=dict(os.environ), timeout=timeout)
    if status:
        raise subprocess.CalledProcessError(status, command)


def contents(path):
    return path.read_bytes() if path.exists() else None


def live_x(kernel):
    result = kernel.session.feed_start("x")
    while isinstance(result, sidecar.NameLookupSnapshot):
        result = result.resume()
    assert isinstance(result, sidecar.MontyComplete), result
    return result.output


def restored(state, expected_names, expected_x):
    # A separate interpreter must load the files before any successful checkpoint.
    requests = [
        {"id": 1, "operation": "variables"},
        {"id": 2, "code": "x"},
        {"id": 3, "operation": "shutdown"},
    ]
    process = runner.run_owned_capture(
        [sys.executable, str(ROOT / "sidecar.py"), "--state", str(state)],
        input="".join(json.dumps(request) + "\n" for request in requests),
        cwd=ROOT, timeout=CHILD_TIMEOUT,
        env={**os.environ, "PI_RLM_MAX_MEMORY": "8000000"},
    )
    assert process.returncode == 0, process.stderr
    messages = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(messages) == 4 and messages[0] == {"ready": True}, messages
    assert messages[1]["ok"] and messages[1]["result"]["names"] == expected_names, messages
    if expected_x is None:
        assert not messages[2]["ok"] and "NameError" in messages[2]["error"], messages
    else:
        assert messages[2]["ok"] and messages[2]["result"]["result"] == expected_x, messages
    assert messages[3]["ok"], messages


def failed_response(state, boundary):
    # Exercise main()'s real JSONL error response too, not only Kernel.execute().
    process = runner.run_owned_capture(
        [sys.executable, str(ROOT / "recovery_check.py"), "--failure-worker", str(state), boundary],
        input=json.dumps({"id": 1, "code": "x = 99\nnew_name = 'tiny'\nx"}) + "\n"
        + json.dumps({"id": 2, "operation": "shutdown"}) + "\n",
        cwd=ROOT, timeout=CHILD_TIMEOUT,
        env={**os.environ, "PI_RLM_MAX_MEMORY": "8000000"},
    )
    assert process.returncode == 0, process.stderr
    messages = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(messages) == 3 and messages[0] == {"ready": True}, messages
    assert messages[1]["id"] == 1 and not messages[1]["ok"] and "Error" in messages[1]["error"], messages
    assert messages[2]["ok"], messages


@contextlib.contextmanager
def fail_at(boundary, state):
    raw_temp = state.with_suffix(".tmp")
    names = state.with_suffix(".names.json")
    names_temp = names.with_suffix(".tmp")
    write_bytes, write_text, replace = Path.write_bytes, Path.write_text, os.replace
    reached = []

    def bytes_failure(path, data):
        if boundary == "raw-write" and path == raw_temp:
            reached.append(boundary)
            write_bytes(path, data[:1])
            raise OSError("injected raw-write failure")
        return write_bytes(path, data)

    def text_failure(path, data, *args, **kwargs):
        if boundary == "names-write" and path == names_temp:
            reached.append(boundary)
            write_text(path, data[:1], *args, **kwargs)
            raise OSError("injected names-write failure")
        return write_text(path, data, *args, **kwargs)

    def replace_failure(source, destination):
        target = state if boundary == "raw-replace" else names
        if boundary.endswith("replace") and Path(destination) == target:
            reached.append(boundary)
            # Reject the actual replacement without changing the destination.
            raise OSError(f"injected {boundary} failure")
        return replace(source, destination)

    with patch.object(Path, "write_bytes", bytes_failure), patch.object(Path, "write_text", text_failure), patch.object(os, "replace", replace_failure):
        yield reached
    assert reached == [boundary], reached


class RecoveryChecks(unittest.TestCase):
    def test_capture_streams(self):
        # Keep the capture oracle in the default narrow/suite path, with its own
        # backup scope rather than extending the original runner self-test.
        status = runner.run_owned([sys.executable, str(ROOT.parent / "isolated_check_tests.py"),
                                   runner.__file__, "--capture-only"],
                                  cwd=ROOT, env=dict(os.environ), timeout=8)
        self.assertEqual(status, 0)

    def test_stalled_workers(self):
        from recovery_timeout_check import check_timeouts
        check_timeouts(self)

    def test_failed_checkpoint(self):
        cases = [(prior, boundary) for prior in ("seeded", "untracked", "empty") for boundary in BOUNDARIES]
        cases.append(("seeded", "directory"))
        passed = 0
        for prior, boundary in cases:
            with self.subTest(prior=prior, boundary=boundary), tempfile.TemporaryDirectory(prefix="pi-rlm-recovery-") as directory:
                state = Path(directory) / "private" / "kernel.bin"
                names = state.with_suffix(".names.json")
                raw_temp = state.with_suffix(".tmp")
                names_temp = names.with_suffix(".tmp")
                kernel = sidecar.Kernel(state)
                try:
                    expected_x = None if prior == "empty" else 40
                    if expected_x is not None:
                        kernel.execute(0, "x = 40\nx", [])
                    if prior == "untracked":
                        names.unlink()
                    previous_raw, previous_names = contents(state), contents(names)
                    live_names = kernel.variables()["names"]
                    if boundary == "directory":
                        names_temp.mkdir()
                        failure = contextlib.nullcontext()
                    else:
                        failure = fail_at(boundary, state)
                    output = io.StringIO()
                    with failure, contextlib.redirect_stdout(output):
                        try:
                            result = kernel.execute(1, "x = 99\nnew_name = 'tiny'\nx", [])
                        except OSError as exc:
                            sidecar.respond(1, error=f"{type(exc).__name__}: {exc}")
                        else:
                            sidecar.respond(1, result=result)
                    response = json.loads(output.getvalue())
                    self.assertFalse(response["ok"], response)
                    self.assertIn("error", response)
                    if expected_x is None:
                        with self.assertRaisesRegex(Exception, "NameError"):
                            live_x(kernel)
                    else:
                        self.assertEqual(live_x(kernel), 40)
                    self.assertEqual(kernel.variables()["names"], live_names)
                    # Raw bytes are authoritative; names are convenience metadata.
                    self.assertEqual(contents(state), previous_raw)
                    expected_metadata = None if boundary == "raw-replace" else previous_names
                    self.assertEqual(contents(names), expected_metadata)
                    self.assertFalse(raw_temp.exists(), "partial raw staging file leaked")
                    if boundary == "directory":
                        self.assertTrue(names_temp.is_dir(), "must not delete the injected obstacle")
                    else:
                        self.assertFalse(names_temp.exists(), "partial name staging file leaked")
                    failed_response(state, boundary)
                    self.assertEqual(contents(state), previous_raw)
                    self.assertEqual(contents(names), expected_metadata)
                    if boundary == "directory":
                        names_temp.rmdir()
                    self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
                    for path in (state, names):
                        if path.exists():
                            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                    expected_names = json.loads(expected_metadata) if expected_metadata is not None else []
                    restored(state, expected_names, expected_x)
                    # Recovery leaves the live kernel usable too, with normal success unchanged.
                    self.assertEqual(kernel.execute(2, "x = 41\nx", [])["result"], 41)
                    self.assertEqual(names.read_bytes(), b'["x"]')
                finally:
                    kernel.close()
                restored(state, ["x"], 41)
                passed += 1
        print(f"rlm-kernel checkpoint failure/restore cases passed: {passed}/{len(cases)}")

    def test_success_and_empty_names(self):
        with tempfile.TemporaryDirectory(prefix="pi-rlm-recovery-") as directory:
            state = Path(directory) / "private" / "kernel.bin"
            kernel = sidecar.Kernel(state)
            order = []
            replace = os.replace

            def record_replace(source, destination):
                order.append(Path(destination))
                return replace(source, destination)

            try:
                with patch.object(os, "replace", record_replace):
                    self.assertEqual(kernel.execute(1, "40", [])["result"], 40)
                self.assertEqual(order, [kernel.names_path, state], "raw replacement must be the final commit")
                self.assertEqual(state.read_bytes(), kernel.session.dump(), "checkpoint is still a raw Monty dump")
                self.assertEqual(kernel.names_path.read_bytes(), b"[]")
                # Even a valid prior checkpoint with empty names survives a failed write.
                raw, metadata = state.read_bytes(), kernel.names_path.read_bytes()
                with fail_at("names-write", state), self.assertRaises(OSError):
                    kernel.execute(2, "x = 99", [])
                self.assertEqual(state.read_bytes(), raw)
                self.assertEqual(kernel.names_path.read_bytes(), metadata)
                restored(state, [], None)
                kernel.execute(3, "x = 40", [])
                for path in (state, kernel.names_path):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            finally:
                kernel.close()
            restored(state, ["x"], 40)
        print("rlm-kernel successful raw-format/private-mode/empty-name restore checks passed")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--failure-worker":
        state, boundary = Path(sys.argv[2]), sys.argv[3]
        sys.argv = [str(ROOT / "sidecar.py"), "--state", str(state)]
        failure = contextlib.nullcontext() if boundary == "directory" else fail_at(boundary, state)
        with failure:
            sidecar.main()
    else:
        unittest.main()
