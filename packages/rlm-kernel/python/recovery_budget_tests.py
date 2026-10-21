"""Dependency-free controls for recovery dispatch; real cases remain in check.py."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
# These controls never call Kernel: import the dispatcher without installing Monty.
spec = importlib.util.spec_from_file_location("recovery_budget_subject", ROOT / "recovery_check.py")
recovery = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"sidecar": types.ModuleType("sidecar")}):
    spec.loader.exec_module(recovery)

EXPECTED = (
    ("capture", 8, ("test_capture_streams",)),
    ("checkpoint", 20, ("test_failed_checkpoint", "test_success_and_empty_names")),
    ("stalled", 8, ("test_stalled_workers",)),
)


class BudgetChecks(unittest.TestCase):
    def test_contract_and_child_deadline(self):
        self.assertEqual(recovery.RECOVERY_GROUPS, EXPECTED)
        self.assertEqual(recovery.CHILD_TIMEOUT, 5)
        recovery.validate_groups()

    def test_invalid_groups_fail_before_launch(self):
        invalid = {
            "missing": EXPECTED[:-1],
            "duplicate": EXPECTED + (EXPECTED[0],),
            "unknown": EXPECTED + (("unknown", 8, ()),),
            "omitted_method": (EXPECTED[0], ("checkpoint", 20, ("test_failed_checkpoint",)), EXPECTED[2]),
            "duplicate_method": (EXPECTED[0], ("checkpoint", 20, ("test_failed_checkpoint",) * 2), EXPECTED[2]),
            "unknown_method": (EXPECTED[0], ("checkpoint", 20, ("test_missing", "test_success_and_empty_names")), EXPECTED[2]),
            "wrong_budget": (("capture", 20, EXPECTED[0][2]), *EXPECTED[1:]),
        }
        for kind, groups in invalid.items():
            with self.subTest(kind=kind), patch.object(recovery, "RECOVERY_GROUPS", groups):
                with patch.object(recovery.runner, "run_owned", side_effect=AssertionError("invalid plan launched")):
                    with self.assertRaises(ValueError):
                        recovery.run_checks()

    def test_omitted_real_method_fails(self):
        with patch.object(recovery.RecoveryChecks, "test_success_and_empty_names", None):
            with self.assertRaises(ValueError):
                recovery.validate_groups()

    def test_unassigned_real_method_fails(self):
        with patch.object(recovery.RecoveryChecks, "test_extra", lambda self: None, create=True):
            with self.assertRaises(ValueError):
                recovery.validate_groups()

    def dispatch_fixture(self, failure=None):
        calls = []
        original = recovery.runner.run_owned
        with tempfile.TemporaryDirectory(prefix="pi-recovery-budget-") as directory:
            record = Path(directory) / "calls.jsonl"

            def launch(command, *, cwd, env, timeout):
                index = len(calls)
                calls.append((command, timeout))
                group, budget, methods = EXPECTED[index]
                self.assertEqual(command, [sys.executable, str(ROOT / "recovery_check.py"),
                                          *("RecoveryChecks." + method for method in methods)])
                self.assertEqual(timeout, budget)
                self.assertEqual(cwd, ROOT)
                code = ("import json, os, pathlib, time; "
                        f"p=pathlib.Path({str(record)!r}); "
                        f"p.open('a').write(json.dumps([{group!r}, os.getpid()])+'\\n'); ")
                if failure == (group, "timeout"):
                    code += "time.sleep(60)"
                    limit = .2
                else:
                    code += "raise SystemExit(7)" if failure == (group, "nonzero") else "pass"
                    limit = timeout
                return original([sys.executable, "-B", "-c", code], cwd=cwd, env=env, timeout=limit)

            started = time.monotonic()
            with patch.object(recovery.runner, "run_owned", launch):
                if failure:
                    with self.assertRaises(subprocess.CalledProcessError) as caught:
                        recovery.run_checks()
                    self.assertEqual(caught.exception.returncode, 124 if failure[1] == "timeout" else 7)
                    self.assertEqual(caught.exception.cmd, calls[-1][0])
                else:
                    recovery.run_checks()
            records = [json.loads(line) for line in record.read_text().splitlines()]
            expected_groups = [group for group, _, _ in EXPECTED]
            if failure:
                expected_groups = expected_groups[:expected_groups.index(failure[0]) + 1]
            self.assertEqual([group for group, _ in records], expected_groups)
            self.assertEqual(len(calls), len(expected_groups), "failure retried or continued")
            for _, pid in records:
                self.assertIsNone(recovery.runner.process_info(pid), "fixture survived BEFORE backup")
            self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(Path(directory).exists())

    def test_default_runs_all_groups_once(self):
        self.dispatch_fixture()

    def test_nonzero_failstop_at_each_group(self):
        for group, _, _ in EXPECTED:
            with self.subTest(group=group):
                self.dispatch_fixture((group, "nonzero"))

    def test_timeout_failstop_at_each_group(self):
        for group, _, _ in EXPECTED:
            with self.subTest(group=group):
                self.dispatch_fixture((group, "timeout"))

    def test_explicit_command_and_timeout_unchanged(self):
        recovery.run_checks([sys.executable, "-B", "-c", "pass"], timeout=.7)
        command = [sys.executable, "-B", "-c", "raise SystemExit(7)"]
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            recovery.run_checks(command, timeout=.7)
        self.assertEqual(caught.exception.returncode, 7)
        self.assertEqual(caught.exception.cmd, command)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            recovery.run_checks([sys.executable, "-B", "-c", "import time; time.sleep(60)"], timeout=.7)
        self.assertEqual(caught.exception.returncode, 124)


if __name__ == "__main__":
    unittest.main(failfast=True)
