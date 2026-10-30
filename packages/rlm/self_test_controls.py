#!/usr/bin/env python3
"""Dispatch-ledger and offline-isolation controls, including wrapper/CLI fixtures."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


class OfflineControls(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory(prefix="pi-offline-control-") as directory:
            root = Path(directory)
            (root / "bin").mkdir()
            (root / "cache").mkdir()
            (root / "personal-agent").mkdir()
            poison = root / "personal-agent" / "settings.json"
            poison.write_text('{"packages":["must-not-load"]}')
            fake_pi = root / "bin" / "fake-pi"
            fake_pi.write_text(f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
                "assert os.environ.get('UV_OFFLINE') == '1', 'wrapper lost offline mode'\n"
                "Path(sys.argv[-1]).write_text(json.dumps({'argv':sys.argv[1:-1], 'cwd':os.getcwd(), "
                "'env':{'PI_CODING_AGENT_DIR':os.environ['PI_CODING_AGENT_DIR']}}))\n")
            fake_pi.chmod(0o700)
            record = root / "uv.jsonl"
            fake_uv = root / "bin" / "uv"
            fake_uv.write_text(f"#!{sys.executable}\nimport json, os, sys\n"
                f"with open({str(record)!r}, 'a') as out:\n"
                "    out.write(json.dumps({'mode':os.environ.get('UV_OFFLINE'), 'argv':sys.argv[1:]})+'\\n')\n"
                "print('simulated cache miss', file=sys.stderr)\n"
                "sys.exit(7 if os.environ.get('UV_OFFLINE') == '1' else 1)\n")
            fake_uv.chmod(0o700)
            source = {"PATH": str(root / "bin") + os.pathsep + os.defpath,
                      "PI_BIN": str(fake_pi), "UV_CACHE_DIR": str(root / "cache"), "UV_OFFLINE": "1",
                      "PI_CODING_AGENT_DIR": str(poison.parent), "PI_SESSION_FILE": "must-not-leak",
                      "OPENAI_API_KEY": "must-not-leak", "AWS_PROFILE": "must-not-leak",
                      "NODE_OPTIONS": "--require must-not-load", "PYTHONPATH": "must-not-load",
                      "UV_INDEX_URL": "must-not-leak", "UV_TOKEN": "must-not-leak",
                      "UV_PROJECT_ENVIRONMENT": "must-not-use", "UV_NO_SYNC": "must-not-use"}
            yield root, source, record
            self.assertEqual(poison.read_text(), '{"packages":["must-not-load"]}')
        self.assertFalse(root.exists())

    def assert_scope(self, env, source, roots):
        private = Path(env["PI_CODING_AGENT_DIR"]).parent
        self.assertNotIn(private, roots)
        roots.append(private)
        for name in ("agent", "state", "sessions", "cwd", "home", "tmp", "config", "cache"):
            self.assertEqual((private / name).stat().st_mode & 0o777, 0o700)
            self.assertFalse(list((private / name).iterdir()))
        for key, name in (("PI_CODING_AGENT_DIR", "agent"), ("PI_RLM_STATE_DIR", "state"),
                          ("PI_CODING_AGENT_SESSION_DIR", "sessions"), ("HOME", "home")):
            self.assertEqual(env[key], str(private / name))
        self.assertEqual(env["UV_PROJECT_ENVIRONMENT"], str(private / "venv"))
        self.assertEqual(env["UV_CACHE_DIR"], source["UV_CACHE_DIR"])
        self.assertEqual(env.get("UV_OFFLINE"), "1")
        self.assertEqual({key for key in env if key.startswith("UV_")},
                         {"UV_OFFLINE", "UV_CACHE_DIR", "UV_PROJECT_ENVIRONMENT"})
        for key in ("PI_SESSION_FILE", "OPENAI_API_KEY", "AWS_PROFILE", "NODE_OPTIONS", "PYTHONPATH"):
            self.assertNotIn(key, env)
        self.assertEqual(env["HINDSIGHT_DISABLED"], "true")
        self.assertEqual(env["PI_OFFLINE"], "1")
        return private

    def assert_cache_miss(self, result, records):
        # Check the actual executable's mode/argv before status: a nonzero exit
        # alone cannot distinguish cache-only failure from lost offline mode.
        self.assertEqual(records, [{"mode": "1", "argv": ["run", "--locked", "python", "-c", "pass"]}])
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertIn("simulated cache miss", result.stderr)
        self.assertRegex(result.stderr, r"[1-9][0-9]* owned PIDs observed, 0 survivors")

    def cache_miss(self, env, record):
        before = record.read_text().splitlines() if record.exists() else []
        result = runner.run_owned_capture(["uv", "run", "--locked", "python", "-c", "pass"],
            cwd=Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd", env=env, timeout=2)
        records = [json.loads(line) for line in record.read_text().splitlines()[len(before):]]
        return result, records

    def test_offline_top_and_nested_cache_only_private_scopes(self):
        with self.fixture() as (root, source, record):
            roots = []
            with runner.isolated_environment(source) as env:
                private = self.assert_scope(env, source, roots)
                self.assert_cache_miss(*self.cache_miss(env, record))
                # Exercise the existing wrapper helpers, including their private
                # cwd/agent and explicit versus discovery flag assertions.
                for discover in (None, False, True):
                    cases.wrapper_case(runner, root, env, private, discover, source)
                for discover in (False, True):
                    with runner.isolated_environment(env, discover_extensions=discover) as nested:
                        self.assert_scope(nested, source, roots)
                        self.assert_cache_miss(*self.cache_miss(nested, record))
                    self.assertFalse(roots[-1].exists())
            self.assertTrue(all(not private.exists() for private in roots))
            self.assertEqual(len(record.read_text().splitlines()), 3)
            print("offline scopes: 3 cache-miss status7 launches, one attempt each; fresh roots/cwd/agent absent, 0 owned survivors", flush=True)

    def test_absent_or_nonaccepted_mode_not_invented(self):
        with self.fixture() as (_root, source, _record):
            for mode in (None, "0", "true"):
                with self.subTest(mode=mode):
                    source.pop("UV_OFFLINE", None)
                    if mode is not None:
                        source["UV_OFFLINE"] = mode
                    with runner.isolated_environment(source) as env:
                        private = Path(env["PI_CODING_AGENT_DIR"]).parent
                        self.assertNotIn("UV_OFFLINE", env)
                        with runner.isolated_environment(env) as nested:
                            nested_root = Path(nested["PI_CODING_AGENT_DIR"]).parent
                            self.assertNotIn("UV_OFFLINE", nested)
                        self.assertFalse(nested_root.exists())
                    self.assertFalse(private.exists())

    def test_lost_and_altered_nested_mode_mutants_rejected(self):
        original = runner.isolated_environment
        for mode in (None, "0"):
            @contextmanager
            def mutant(source, *, mode=mode, **kwargs):
                with original(source, **kwargs) as env:
                    if "_PI_ISOLATED_WRAPPER" in source:
                        env.pop("UV_OFFLINE", None)
                        if mode is not None:
                            env["UV_OFFLINE"] = mode
                    yield env
            with self.subTest(mode=mode), self.fixture() as (_root, source, record):
                with patch.object(runner, "isolated_environment", mutant):
                    with runner.isolated_environment(source) as env:
                        private = Path(env["PI_CODING_AGENT_DIR"]).parent
                        self.assertEqual(env.get("UV_OFFLINE"), "1")
                        with runner.isolated_environment(env) as nested:
                            nested_root = Path(nested["PI_CODING_AGENT_DIR"]).parent
                            result, records = self.cache_miss(nested, record)
                            self.assertEqual(len(records), 1)
                            self.assertEqual(records[0]["mode"], mode)
                            self.assertEqual(result.returncode, 1)
                            self.assertIn("0 survivors", result.stderr)
                            with self.assertRaises(AssertionError):
                                self.assert_cache_miss(result, records)
                        self.assertFalse(nested_root.exists())
                    self.assertFalse(private.exists())
                print(f"offline mutant rejected: nested mode={mode!r}, status1, one attempt, roots absent", flush=True)

    def test_actual_cli_nested_cache_miss_status_propagation(self):
        guard = r'''
import importlib.util, json, os, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
outer = Path(os.environ['PI_CODING_AGENT_DIR']).parent
with runner.isolated_environment(discover_extensions=True) as env:
    private = Path(env['PI_CODING_AGENT_DIR']).parent
    assert private != outer
    result = runner.run_owned_capture(['uv', 'run', '--locked', 'python', '-c', 'pass'],
        cwd=private / 'cwd', env=env, timeout=2)
    sys.stderr.write(result.stderr)
assert not private.exists(), 'nested private root survived before backup'
print(json.dumps([str(outer), str(private)]), flush=True)
sys.exit(result.returncode)
'''
        with self.fixture() as (root, source, record):
            result = runner.run_owned_capture([sys.executable, runner.__file__, "--timeout", "2", "--",
                sys.executable, "-c", guard, runner.__file__], cwd=root, env=source, timeout=6)
            self.assert_cache_miss(result, [json.loads(line) for line in record.read_text().splitlines()])
            roots = json.loads(result.stdout)
            self.assertEqual(len(set(roots)), 2)
            self.assertTrue(all(not Path(private).exists() for private in roots))
            self.assertEqual(result.stderr.count("0 survivors"), 3)
            print("offline CLI: nested cache-miss status7 propagated; one uv run --locked, no attempt2; roots absent, 0 owned survivors", flush=True)


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
