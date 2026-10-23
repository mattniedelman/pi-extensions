#!/usr/bin/env python3
"""Offline dispatch controls; synthetic callbacks exercise only the test ledger."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
ORIGINAL = (
    "unrelated-adoption", "dynamic-unrelated-adoption", "real-pi-poison-isolation",
    "environment-caller-cwd", "explicit-wrapper", "nested-explicit-wrapper",
    "nested-discovery-wrapper", "tree-success", "tree-exit7", "tree-failure",
    "tree-eof", "tree-hang", "missing-command", "private-root-poison-unchanged",
    "outer-TERM", "outer-INT", "outer-HUP", "safety-thread", "safety-termfork",
    "late-caller-TERM", "late-caller-INT", "late-caller-HUP",
    "late-supervisor-TERM", "late-supervisor-INT", "late-supervisor-HUP",
    "exit125-precedence", "cleanup125-precedence", "caller-after-exit0",
    "caller-after-exit125",
)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cases = load("self_test_cases", "isolated_check_tests.py")
runner = load("self_test_runner", "isolated-check.py")


def verify_log(path):
    lines = Path(path).read_text().splitlines()
    records = [json.loads(line.removeprefix("CASE_RESULT ")) for line in lines
               if line.startswith("CASE_RESULT ")]
    assert tuple(record["name"] for record in records) == ORIGINAL
    assert len({record["fixture"] for record in records}) == 29
    assert len({(record["pid"], record["identity"]) for record in records}) == 29
    for record in records:
        assert record["status"] == 0 and record["fixtureCleanup"] is True, record
        assert not Path(record["fixture"]).exists(), record
        info = runner.process_info(record["pid"])
        assert not (info and info[0] == record["identity"]), record
    assert any("29/29 original scenarios passed" in line for line in lines)
    print("actual execution evidence: 29/29 unique cases, process identities and removed private fixtures")


class DispatchControls(unittest.TestCase):
    def test_fixed_inventory_and_budgets(self):
        registry = cases.validate_registry()
        self.assertEqual(tuple(registry), ORIGINAL)
        self.assertTrue(all(case[1] > 0 for case in registry.values()))
        self.assertGreater(cases.total_budget(tuple(registry.values())), 15)

    def test_missing_duplicate_unknown_registration(self):
        registry = cases.CASES
        for broken in (registry[:-1], registry + (registry[0],),
                       registry[:-1] + (("invented", *registry[-1][1:]),)):
            with self.subTest(names=[case[0] for case in broken]):
                with self.assertRaises(ValueError):
                    cases.validate_registry(broken)

    def test_unknown_and_duplicate_selection_before_execution(self):
        with patch.object(cases, "execute_case", side_effect=AssertionError("must not execute")):
            for selected in (("invented",), (ORIGINAL[0], ORIGINAL[0])):
                with self.assertRaises(ValueError):
                    cases.self_test(runner, selected)

    def test_inert_import_list_budget(self):
        with patch.object(cases.tempfile, "TemporaryDirectory", side_effect=AssertionError("fixture effect")), \
             patch.object(cases.subprocess, "Popen", side_effect=AssertionError("process effect")):
            module = load("inert_cases", "isolated_check_tests.py")
            selected = module.select_cases(["explicit-wrapper"])
            self.assertGreater(module.total_budget(selected), 0)
        for flag in ("--list-cases", "--self-test-budget"):
            result = subprocess.run([sys.executable, str(ROOT / "isolated-check.py"), flag],
                                    text=True, capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)
            if flag == "--self-test-budget":
                self.assertEqual(int(result.stdout), cases.total_budget(cases.select_cases()))
            else:
                data = json.loads(result.stdout)
                self.assertEqual(tuple(case["name"] for case in data["cases"]), ORIGINAL)
                self.assertEqual(data["totalBudgetSeconds"], cases.total_budget(cases.select_cases()))

    def test_synthetic_omission_dropped_ledger_and_nonzero(self):
        # This callback is deliberately synthetic, not evidence of scenario PASS.
        for record in (None, {"name": ORIGINAL[0], "status": 7},
                       {"name": ORIGINAL[0], "status": 124}):
            with self.subTest(record=record), patch.object(cases, "execute_case", return_value=record):
                self.assertNotEqual(cases.self_test(runner, [ORIGINAL[0]]), 0)
        success = {"name": ORIGINAL[0], "status": 0, "fixtureCleanup": True,
                   "fixture": "synthetic-only", "pid": 1, "identity": "synthetic-only"}
        output = io.StringIO()
        with patch.object(cases, "execute_case", side_effect=[success, None]) as execute, \
             redirect_stderr(output), redirect_stdout(output):
            self.assertEqual(cases.self_test(runner), 125)
        self.assertEqual(execute.call_count, 2)
        self.assertIn("remaining not run=", output.getvalue())
        self.assertNotIn("29/29 original scenarios passed", output.getvalue())
        with self.assertRaises(ValueError):
            cases.check_results(cases.select_cases(), [], full=True)
        with self.assertRaises(ValueError):
            cases.check_results(cases.select_cases(), [{"name": name, "status": 0} for name in ORIGINAL[:-1]], full=True)
        records = [{"name": name, "status": 0} for name in ORIGINAL]
        for broken in (records + [records[0]], records[:-1] + [records[0]], records[::-1]):
            with self.assertRaises(ValueError):
                cases.check_results(cases.select_cases(), broken, full=True)

    def test_actual_subset_independent_not_full_pass(self):
        result = subprocess.run([sys.executable, str(ROOT / "isolated-check.py"),
                                 "--case", "explicit-wrapper", "--case", "tree-exit7"],
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        records = [json.loads(line.removeprefix("CASE_RESULT ")) for line in result.stdout.splitlines()
                   if line.startswith("CASE_RESULT ")]
        self.assertEqual([r["name"] for r in records], ["explicit-wrapper", "tree-exit7"])
        self.assertEqual(len({r["fixture"] for r in records}), 2)
        self.assertEqual(len({(r["pid"], r["identity"]) for r in records}), 2)
        self.assertTrue(all(r["status"] == 0 and r["fixtureCleanup"] and not Path(r["fixture"]).exists() for r in records))
        self.assertIn("not full-suite PASS", result.stdout)
        self.assertNotIn("29/29 original scenarios passed", result.stdout)

    def test_parent_term_cleans_current_case_not_unrelated_caller_child(self):
        # Dedicated backup owns this control only. Assertions precede its exit.
        guard = r'''
import importlib.util, os, signal, subprocess, sys, tempfile, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
with tempfile.TemporaryDirectory(prefix='pi-dispatch-term-') as directory:
    root = Path(directory)
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    sentinel_identity = runner.process_info(sentinel.pid)[0]
    try:
        with (root / 'output').open('w+') as output:
            proc = subprocess.Popen([sys.executable, runner.__file__, '--case', 'tree-hang'], stdout=output)
            deadline = time.monotonic() + 3
            scope = None
            while time.monotonic() < deadline:
                output.seek(0)
                lines = output.read().splitlines()
                if lines and lines[0].startswith('SUITE_SCOPE '):
                    scope = Path(lines[0].removeprefix('SUITE_SCOPE '))
                    if list(scope.glob('pi-self-case-*/case-*/hang.child')):
                        break
                time.sleep(.002)
            else:
                raise AssertionError('case never ready')
            identities = []
            pending = [proc.pid]
            while pending:
                pid = pending.pop()
                info = runner.process_info(pid)
                if info:
                    identities.append((pid, info[0]))
                    pending.extend(runner.children(pid))
            assert len(identities) >= 7, identities
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=3) == 143
            assert not scope.exists(), 'suite filesystem backup did not run'
            for pid, identity in identities:
                info = runner.process_info(pid)
                assert not (info and info[0] == identity), ('owned survivor', pid, identity)
            assert sentinel.poll() is None and runner.process_info(sentinel.pid)[0] == sentinel_identity
            print('parent TERM: %s owned identities absent, private scope absent, unrelated caller child alive' % len(identities))
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=1)
'''
        result = runner.run_owned_capture([sys.executable, "-c", guard, runner.__file__],
                                          cwd=Path.cwd(), env=dict(os.environ), timeout=6)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("unrelated caller child alive", result.stdout)
        print(result.stdout, end="", flush=True)

    def test_actual_exit7_timeout_and_missing_receipt(self):
        case = cases.select_cases(["explicit-wrapper"])[0]
        # Real processes, synthetic command bodies: no original handler or runtime policy is changed.
        for code, budget, expected in (("raise SystemExit(7)", 1, 7),
                                       ("import time; time.sleep(30)", .1, 124),
                                       ("pass", 1, 125)):
            with self.subTest(expected=expected):
                with patch.object(cases, "case_command", return_value=[sys.executable, "-c", code]):
                    record = cases.execute_case(runner, (case[0], budget, *case[2:]))
                self.assertEqual(record["status"], expected)
                with patch.object(cases, "execute_case", return_value=record):
                    self.assertEqual(cases.self_test(runner, [case[0]]), expected)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify-log")
    args = parser.parse_args()
    if args.verify_log:
        verify_log(args.verify_log)
    else:
        unittest.main(argv=[sys.argv[0]])
