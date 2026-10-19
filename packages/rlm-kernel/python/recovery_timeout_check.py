"""Stall real Monty workers and check absence before the fixture backup runs."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("isolated_runner", ROOT.parent / "isolated-check.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

STALLED = r'''
import importlib.util, json, os, signal, sys
from pathlib import Path
sys.path.insert(0, ROOT)
import sidecar
spec = importlib.util.spec_from_file_location('runner', RUNNER)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
original = sidecar.Kernel
class StalledKernel(original):
    def __init__(self, state):
        super().__init__(state)
        workers = runner.children(os.getpid())
        assert workers, 'no real Monty worker'
        records = [{'pid': pid, 'identity': runner.process_info(pid)[0]} for pid in workers]
        records.append({'pid': os.getpid(), 'identity': runner.process_info(os.getpid())[0]})
        Path(RECORD).write_text(json.dumps(records))
        for pid in workers:
            os.kill(pid, signal.SIGSTOP)
sidecar.Kernel = StalledKernel
if len(sys.argv) > 1 and sys.argv[1] == '--failure-worker':
    sys.argv = [__file__, '--state', sys.argv[2]]
sidecar.main()
'''


def oracle(kind, directory):
    import recovery_check as recovery
    record = directory / "identities.json"
    fixture = "ROOT = %r\nRUNNER = %r\nRECORD = %r\n" % (
        str(ROOT), runner.__file__, str(record)) + STALLED
    for name in ("sidecar.py", "recovery_check.py"):
        (directory / name).write_text(fixture)
    state = directory / "private" / "kernel.bin"
    # This caller already owns a live kernel. Its worker must not be included in
    # the recovery supervisor's scope, even when the stalled child is adopted.
    sentinel = recovery.sidecar.Kernel(directory / "sentinel" / "kernel.bin")
    try:
        sentinel.execute(0, "x = 40", [])
        started = time.monotonic()
        with patch.object(recovery, "ROOT", directory), patch.object(recovery, "CHILD_TIMEOUT", .7):
            try:
                if kind == "restore":
                    recovery.restored(state, [], None)
                elif kind == "failure":
                    recovery.failed_response(state, "raw-write")
                else:
                    recovery.run_checks([sys.executable, str(directory / "sidecar.py"),
                                         "--state", str(state)], timeout=.7)
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError, AssertionError) as error:
                elapsed = time.monotonic() - started
                assert .7 <= elapsed < 3, (kind, elapsed)
                records = json.loads(record.read_text())
                surviving = [data for data in records if (info := runner.process_info(data['pid']))
                             and info[0] == data['identity']]
                print(f"TIMEOUT {kind}: {type(error).__name__}, {elapsed:.3f}s, "
                      f"surviving identities BEFORE backup: {surviving}", flush=True)
                assert not surviving, (kind, surviving)
                assert recovery.live_x(sentinel) == 40, "cleanup touched caller's live Monty worker"
            else:
                raise AssertionError("stalled worker unexpectedly completed")
    finally:
        sentinel.close()


def check_timeouts(test):
    passed = 0
    for kind in ("restore", "failure", "guard"):
        with test.subTest(kind=kind), tempfile.TemporaryDirectory(prefix="pi-rlm-stall-") as directory:
            # Backup only owns this oracle; the absence assertions run inside it
            # before exit. Red tests cannot leave a stopped worker behind.
            status = runner.run_owned([sys.executable, str(Path(__file__).resolve()), "--oracle",
                                       kind, directory], cwd=ROOT, env=dict(os.environ), timeout=4)
            test.assertEqual(status, 0, kind)
            passed += 1
    print(f"rlm-kernel stalled-worker timeout checks passed: {passed}/3 (absence BEFORE backup)")


class TimeoutChecks(unittest.TestCase):
    def test_stalled_workers(self):
        check_timeouts(self)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--oracle":
        oracle(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
