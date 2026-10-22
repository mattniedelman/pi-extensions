#!/usr/bin/env python3
"""Offline controls for the opt-in live permission oracle; no real configs/model."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("permissions", ROOT / "permissions.py")
permissions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permissions)


class DiagnosticControls(unittest.TestCase):
    def fixture(self):
        from types import SimpleNamespace
        parent = "call_" + "a" * 32 + "|fc_call_" + "a" * 32
        ids = [parent + "/1", parent + "/2"]
        args = {"code": permissions.CELL}
        paths = [{"path": "allowed.txt"}, {"path": "denied.txt"}]
        events = [{"type": "tool_execution_start", "toolName": "python", "toolCallId": parent, "args": args}]
        for index, path in enumerate(paths):
            events.append({"type": "tool_execution_start", "toolName": "read", "toolCallId": ids[index],
                           "parentToolCallId": parent, "args": path})
            events.append({"type": "tool_execution_end", "toolName": "read", "toolCallId": ids[index],
                           "isError": bool(index), "result": {"content": "IUA_DENIED" if index else "IUA_ALLOWED"}})
        events.extend([{"type": "tool_execution_end", "toolName": "python", "toolCallId": parent,
                        "isError": False, "result": {"details": {"result": permissions.EXPECTED, "output": "", "truncated": False}}},
                       {"type": "message_end", "message": {"role": "assistant", "provider": "litellm",
                        "model": "fixture", "stopReason": "stop", "usage": {"totalTokens": 1}}},
                       {"type": "agent_settled", "aborted": False}])
        policy = [{"type": "tool_call", "toolName": "python", "toolCallId": parent, "idMatches": True,
                   "argumentMatches": True, "denialReason": "none"}]
        for index, path in enumerate(paths):
            policy.append({"type": "tool_call", "toolName": "read", "toolCallId": ids[index], "idMatches": True,
                           "argumentMatches": True, "path": path["path"], "denialReason": "IUA_DENIED" if index else "none"})
        policy.extend([{"type": "tool_result", "toolName": "read", "toolCallId": ids[0], "isError": False,
                        "idMatches": True, "argumentMatches": True, "path": "allowed.txt"},
                       {"type": "tool_result", "toolName": "python", "toolCallId": parent, "isError": False,
                        "idMatches": True, "argumentMatches": True}])
        rpc = SimpleNamespace(events=events, privacy_ok=True, protocol_ok=True)
        phases = {"startup": {"allowed.txt": 0, "denied.txt": 0},
                  "beforePrompt": {"allowed.txt": 0, "denied.txt": 0},
                  "afterSettled": {"allowed.txt": 1, "denied.txt": 0},
                  "finalShutdown": {"allowed.txt": 1, "denied.txt": 0}}
        return rpc, policy, phases

    def test_replay_zero_usage_never_satisfies_live_guard(self):
        rpc, policy, phases = self.fixture()
        first = rpc.events[6]["message"]
        first.update(provider="q1s-local-replay", model="fixed-python", stopReason="toolUse",
            usage={"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0,
                   "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}})
        import copy
        second = copy.deepcopy(first)
        second["stopReason"] = "stop"
        rpc.events.insert(-1, {"type": "message_end", "message": second})
        selector = "q1s-local-replay/fixed-python"
        report = permissions.verify(rpc, policy, phases, selector, replay=True)
        self.assertTrue(report["passed"])
        self.assertEqual(report["checks"]["replay_zero_inference"], "PASS")
        self.assertNotIn("model_usage", report["checks"])
        live = permissions.verify(rpc, policy, phases, selector)
        self.assertFalse(live["passed"])
        self.assertEqual(live["checks"]["model_usage"], "FAIL")
        for key, invalid in (("input", 1), ("totalTokens", -1), ("totalTokens", False), ("output", None)):
            with self.subTest(key=key, invalid=invalid):
                clean = first["usage"][key]
                first["usage"][key] = invalid
                self.assertEqual(permissions.verify(rpc, policy, phases, selector, replay=True)
                    ["checks"]["replay_zero_inference"], "FAIL")
                first["usage"][key] = clean
        first["usage"]["cost"]["total"] = 1
        self.assertFalse(permissions.verify(rpc, policy, phases, selector, replay=True)["passed"])
        first["usage"]["cost"]["total"] = 0
        self.assertFalse(permissions.verify(rpc, policy, phases, "litellm/fixture", replay=True)["passed"])
        rpc.events.pop(-2)
        self.assertFalse(permissions.verify(rpc, policy, phases, selector, replay=True)["passed"])

    def test_sensor_failure_does_not_abort_independent_checks(self):
        rpc, policy, phases = self.fixture()
        phases["afterSettled"]["denied.txt"] = phases["finalShutdown"]["denied.txt"] = 1
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"]["sensor"], "FAIL")
        self.assertTrue(all(v == "PASS" for k, v in report["checks"].items() if k != "sensor"))
        self.assertEqual(report["checks"]["actual_python_arguments"], "PASS")
        self.assertEqual(report["checks"]["hook_results"], "PASS")
        self.assertEqual(report["checks"]["python_result_envelope"], "PASS")
        with self.assertRaisesRegex(AssertionError, "^permissions invariant failure$"):
            permissions.assert_report(report)

    def test_valid_record_and_each_original_invariant_is_strict(self):
        rpc, policy, phases = self.fixture()
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertTrue(report["passed"])
        permissions.assert_report(report)
        mutations = {
            "settled": lambda r, p: r.events[7].update(aborted=True),
            "actual_python_arguments": lambda r, p: r.events[0]["args"].update(code="synthetic-secret"),
            "python_call_count": lambda r, p: r.events.append(r.events[0]),
            "execution_counts": lambda r, p: r.events.append({"type": "tool_execution_start", "toolName": "other"}),
            "nested_ids": lambda r, p: r.events[1].update(toolCallId="wrong"),
            "nested_arguments": lambda r, p: r.events[1].update(args={"path": "synthetic-secret"}),
            "nested_parent": lambda r, p: r.events[1].update(parentToolCallId="wrong"),
            "execution_ends": lambda r, p: r.events.pop(2),
            "nested_status": lambda r, p: r.events[4].update(isError=False),
            "allowed_content": lambda r, p: r.events[2].update(result={}),
            "denial_reason": lambda r, p: r.events[4].update(result={}),
            "python_status": lambda r, p: r.events[5].update(isError=True),
            "python_result_envelope": lambda r, p: r.events[5].update(result={"details": {}}),
            "hook_calls": lambda r, p: p.pop(0),
            "hook_ids": lambda r, p: p[0].update(idMatches=False),
            "hook_arguments": lambda r, p: p[0].update(argumentMatches=False),
            "hook_denial_reason": lambda r, p: p[2].update(denialReason="none"),
            "hook_results": lambda r, p: p.pop(),
            "selected_model": lambda r, p: r.events[6]["message"].update(model="synthetic-secret"),
            "model_status": lambda r, p: r.events[6]["message"].update(stopReason="error"),
            "model_usage": lambda r, p: r.events[6]["message"].update(usage={}),
            "rpc_privacy": lambda r, p: setattr(r, "privacy_ok", False),
            "rpc_protocol": lambda r, p: setattr(r, "protocol_ok", False),
        }
        for name, mutate in mutations.items():
            with self.subTest(check=name):
                rpc, policy, phases = self.fixture()
                mutate(rpc, policy)
                report = permissions.verify(rpc, policy, phases, "litellm/fixture")
                self.assertFalse(report["passed"], "broken invariant accepted")
                self.assertEqual(report["checks"][name], "FAIL")
                self.assertNotIn("synthetic-secret", json.dumps(report))

    def test_denied_access_at_any_phase_and_counter_reset_fail(self):
        for phase in ("startup", "beforePrompt", "afterSettled", "finalShutdown"):
            rpc, policy, phases = self.fixture()
            phases[phase]["denied.txt"] = 1
            report = permissions.verify(rpc, policy, phases, "litellm/fixture")
            self.assertEqual(report["checks"]["sensor"], "FAIL")
            self.assertFalse(report["passed"])
        rpc, policy, phases = self.fixture()
        phases["beforePrompt"]["allowed.txt"] = 2
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertEqual(report["checks"]["sensor_monotonic"], "FAIL")
        self.assertFalse(report["passed"])

    def test_unknown_startup_and_protocol_records_report_without_secrets(self):
        from types import SimpleNamespace
        rpc = SimpleNamespace(events=[{"type": "synthetic-secret", "args": "synthetic-secret"},
                                     {"type": "tool_execution_start", "toolName": "synthetic-secret",
                                      "toolCallId": "synthetic-secret", "args": {"code": "synthetic-secret"}}],
                              privacy_ok=True, protocol_ok=False)
        report = permissions.verify(rpc, [], {"startup": {"allowed.txt": 0, "denied.txt": 0}}, "litellm/fixture")
        self.assertFalse(report["passed"])
        self.assertEqual(report["checks"]["rpc_protocol"], "FAIL")
        self.assertEqual(report["unknownEventTypes"], 1)
        self.assertNotIn("synthetic-secret", json.dumps(report))
        self.assertEqual(permissions.safe_error_type(AssertionError("synthetic-secret")), "AssertionError")
        self.assertEqual(permissions.safe_error_type(type("synthetic-secret", (Exception,), {})("secret")), "Error")

    def test_leak_drops_raw_record_but_next_pump_collects_available_evidence(self):
        import time
        from types import SimpleNamespace
        sentinel = "synthetic-secret"
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stdout:
            rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), sentinel)
            try:
                records = [{"type": "extension_error", "error": sentinel}, {"type": "agent_settled", "aborted": False}]
                os.write(write_fd, b"".join(json.dumps(record).encode() + b"\n" for record in records))
                with self.assertRaises(AssertionError):
                    rpc.pump(time.monotonic() + 1)
                rpc.pump(time.monotonic() + 1)
                self.assertFalse(rpc.privacy_ok)
                self.assertEqual(rpc.events, [records[1]])
                self.assertNotIn(sentinel.encode(), rpc.buffer)
            finally:
                rpc.close(); os.close(write_fd)


    def test_malformed_record_is_sticky_protocol_failure(self):
        import time
        from types import SimpleNamespace
        for line in (b'[]', b'{"type":"response","id":[]}', b'{"type":7}', b'{invalid'):
            with self.subTest(line=line):
                read_fd, write_fd = os.pipe()
                with os.fdopen(read_fd, "rb") as stdout:
                    rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), "unused")
                    try:
                        os.write(write_fd, line + b"\n" + b'{"type":"agent_settled","aborted":false}\n')
                        with self.assertRaises(RuntimeError):
                            rpc.pump(time.monotonic() + 1)
                        rpc.pump(time.monotonic() + 1)
                        self.assertFalse(rpc.protocol_ok)
                        self.assertEqual(rpc.events, [{"type": "agent_settled", "aborted": False}])
                    finally:
                        rpc.close(); os.close(write_fd)

    def test_hook_logs_only_allowlisted_paths_matches_ids_and_decisions(self):
        source = """
import factory from %s;
const handlers = {};
factory({on: (name, handler) => {handlers[name] = handler;}});
const parent = 'call_' + 'a'.repeat(32) + '|fc_call_' + 'a'.repeat(32);
handlers.tool_call({toolName:'python', toolCallId:parent, input:{code:process.env.PI_RLM_EXPECTED_CELL}});
handlers.tool_call({toolName:'read', toolCallId:parent+'/1', input:{path:'allowed.txt'}});
handlers.tool_call({toolName:'read', toolCallId:parent+'/2', input:{path:'denied.txt'}});
handlers.tool_result({toolName:'read', toolCallId:parent+'/1', input:{path:'allowed.txt'}, isError:false});
handlers.tool_call({toolName:'synthetic-secret', toolCallId:'synthetic-secret', input:{path:'synthetic-secret'}});
handlers.tool_result({toolName:'python', toolCallId:parent, input:{code:'synthetic-secret'}, isError:false});
""" % json.dumps((ROOT / "permissions-probe.mjs").as_uri())
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            path = Path(directory) / "policy.jsonl"
            result = subprocess.run(["node", "--input-type=module", "-e", source],
                env={**os.environ, "PI_RLM_POLICY_LOG": str(path), "PI_RLM_EXPECTED_CELL": permissions.CELL},
                capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            raw = path.read_text()
            self.assertTrue("synthetic-secret" not in raw, "unsafe hook observation retained")
            events = [json.loads(line) for line in raw.splitlines()]
            self.assertTrue(all(e["argumentMatches"] and e["idMatches"] for e in events[:4]))
            self.assertEqual([e["denialReason"] for e in events[:3]], ["none", "none", "IUA_DENIED"])
            self.assertFalse(events[4]["idMatches"])
            self.assertNotIn("toolCallId", events[4])
            self.assertFalse(events[5]["argumentMatches"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_hook_leaks_and_malformed_records_never_become_pass(self):
        import secrets
        sentinel = "IUA_PRIVATE_" + secrets.token_hex(24)
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            path = Path(directory) / "policy.jsonl"
            leaked = json.dumps({"type": "tool_call", "error": sentinel}).replace(sentinel,
                "\\u%04x" % ord(sentinel[0]) + sentinel[1:])
            safe = {"type": "tool_call", "toolName": "read", "argumentMatches": True}
            path.write_text(leaked + "\n[]\n{invalid\n" + json.dumps(safe) + "\n")
            events, privacy_ok, protocol_ok = permissions.read_policy(path, sentinel)
            self.assertFalse(privacy_ok)
            self.assertFalse(protocol_ok)
            self.assertTrue(events == [safe], "unsafe hook record retained or safe record lost")

    def test_settled_collector_keeps_observing_after_leak_without_retry(self):
        from types import SimpleNamespace
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stdout:
            rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), "synthetic-secret")
            try:
                os.write(write_fd, b'{"type":"extension_error","error":"synthetic-secret"}\n'
                    b'{"type":"agent_settled","aborted":false}\n')
                errors = permissions.collect_settled(rpc, timeout=1)
                self.assertEqual(errors, {"AssertionError"})
                self.assertFalse(rpc.privacy_ok)
                self.assertEqual(rpc.events, [{"type": "agent_settled", "aborted": False}])
            finally:
                rpc.close(); os.close(write_fd)

    def test_allowed_access_must_be_positive_before_and_after_shutdown(self):
        rpc, policy, phases = self.fixture()
        phases["afterSettled"]["allowed.txt"] = 0
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertEqual(report["checks"]["sensor"], "FAIL")

    def test_missing_allowed_baseline_and_unknown_events_fail(self):
        rpc, policy, phases = self.fixture()
        for counts in phases.values():
            counts["allowed.txt"] = 0
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertEqual(report["checks"]["sensor"], "FAIL")
        rpc.events.append({"type": "synthetic-secret"})
        report = permissions.verify(rpc, policy, phases, "litellm/fixture")
        self.assertEqual(report["checks"]["rpc_event_types"], "FAIL")
        self.assertNotIn("synthetic-secret", json.dumps(report))


class Controls(unittest.TestCase):
    def test_no_model_is_explicit_skip(self):
        result = subprocess.run(["python3", str(ROOT / "e2e.py"), "--case", "permissions"],
                                env={k: v for k, v in os.environ.items() if k != "PI_RLM_TEST_MODEL"},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("SKIP", result.stdout)
        self.assertIn("permissions", result.stdout)

    def test_copies_private_and_removed_even_on_failure(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir(mode=0o700)
            for name in permissions.MODEL_FILES:
                (source / name).write_text(json.dumps({"fixture": "private test value"}))
            originals = {name: (source / name).read_bytes() for name in permissions.MODEL_FILES}
            with self.assertRaisesRegex(RuntimeError, "deliberate"):
                with permissions.private_models(source, target):
                    for name in permissions.MODEL_FILES:
                        self.assertEqual((target / name).read_bytes(), originals[name])
                        self.assertEqual(stat.S_IMODE((target / name).stat().st_mode), 0o600)
                    raise RuntimeError("deliberate")
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual({name: (source / name).read_bytes() for name in permissions.MODEL_FILES}, originals)

    def test_model_success_and_exclusive_existing_path(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir(mode=0o700)
            for name in permissions.MODEL_FILES:
                (source / name).write_text('{}')
            with permissions.private_models(source, target):
                self.assertEqual(set(p.name for p in target.iterdir()), set(permissions.MODEL_FILES))
            self.assertEqual(list(target.iterdir()), [])
            # Collision on the second copy must clean the first but retain existing bytes.
            existing = target / permissions.MODEL_FILES[1]
            existing.write_text("existing private fixture")
            with self.assertRaises(FileExistsError):
                with permissions.private_models(source, target):
                    self.fail("existing model overwritten")
            self.assertEqual(list(target.iterdir()), [existing])
            self.assertEqual(existing.read_text(), "existing private fixture")

    def test_partial_copy_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir(mode=0o700)
            (source / permissions.MODEL_FILES[0]).write_text("{}")
            with self.assertRaises(FileNotFoundError):
                with permissions.private_models(source, target):
                    self.fail("missing source accepted")
            self.assertEqual(list(target.iterdir()), [])

    def test_auth_selects_only_litellm_and_cleans_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
                root = Path(directory)
                source, target = root / "source", root / "target"
                source.mkdir(); target.mkdir(mode=0o700)
                selected = {"type": "oauth", "access": "synthetic-selected", "refresh": "synthetic-refresh"}
                original = json.dumps({"other-provider": {"key": "synthetic-unselected"}, "litellm": selected}).encode()
                (source / "auth.json").write_bytes(original)
                try:
                    with permissions.private_litellm_auth(source, target):
                        self.assertEqual(json.loads((target / "auth.json").read_bytes()), {"litellm": selected})
                        self.assertNotIn(b"synthetic-unselected", (target / "auth.json").read_bytes())
                        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
                        self.assertEqual(stat.S_IMODE((target / "auth.json").stat().st_mode), 0o600)
                        # Normal SDK refresh is permitted only on the private copy.
                        (target / "auth.json").write_text('{"litellm":{"access":"synthetic-refreshed"}}')
                        if fail:
                            raise RuntimeError("deliberate body failure")
                except RuntimeError as error:
                    self.assertTrue(fail)
                    self.assertEqual(str(error), "deliberate body failure")
                self.assertFalse((target / "auth.json").exists())
                self.assertEqual((source / "auth.json").read_bytes(), original)

    def test_auth_setup_failures_are_private_and_exclusive(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir(mode=0o700)
            for original in (b'{"litellm": synthetic-secret-invalid}', b'{"other":{"key":"synthetic-unselected"}}',
                             b'{"litellm":null}', b'[]'):
                (source / "auth.json").write_bytes(original)
                with self.subTest(original=original), self.assertRaises(RuntimeError) as caught:
                    with permissions.private_litellm_auth(source, target):
                        self.fail("invalid source accepted")
                self.assertEqual(str(caught.exception), "private LiteLLM auth setup failed")
                self.assertIsNone(caught.exception.__cause__)
                self.assertFalse((target / "auth.json").exists())
                self.assertEqual((source / "auth.json").read_bytes(), original)
            (source / "auth.json").write_text('{"litellm":{"key":"synthetic-selected"}}')
            for mode in (0o755, 0o700):
                target.chmod(mode)
                existing = target / "auth.json"
                existing.write_text("existing private fixture")
                with self.assertRaises(RuntimeError):
                    with permissions.private_litellm_auth(source, target):
                        self.fail("unsafe mode/existing path accepted")
                self.assertEqual(existing.read_text(), "existing private fixture")
                existing.unlink()

    def test_auth_partial_write_cleanup_and_source_change_detection(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir(mode=0o700)
            original = b'{"litellm":{"key":"synthetic-selected"}}'
            (source / "auth.json").write_bytes(original)
            with patch.object(permissions.json, "dump", side_effect=OSError("synthetic-secret-error")):
                with self.assertRaisesRegex(RuntimeError, "^private LiteLLM auth setup failed$"):
                    with permissions.private_litellm_auth(source, target):
                        self.fail("partial write accepted")
            self.assertFalse((target / "auth.json").exists())
            self.assertEqual((source / "auth.json").read_bytes(), original)
            with self.assertRaisesRegex(RuntimeError, "^source auth changed during private check$"):
                with permissions.private_litellm_auth(source, target):
                    (source / "auth.json").write_text("synthetic external modification")
            self.assertFalse((target / "auth.json").exists())
            # The helper must detect, not overwrite, an external source change.
            self.assertEqual((source / "auth.json").read_text(), "synthetic external modification")

    def test_actual_pump_rejects_literal_and_escaped_raw_json_before_sanitizing(self):
        import secrets
        import time
        from types import SimpleNamespace
        sentinel = "IUA_PRIVATE_" + secrets.token_hex(24)
        payloads = {
            "error": {"type": "extension_error", "error": sentinel},
            "nested": {"type": "message_end", "message": {"content": [{"text": sentinel}]}},
            "model": {"type": "response", "id": "state", "data": {"model": {"private": sentinel}}},
            "header": {"type": "response", "id": "state", "headers": {"Authorization": sentinel}},
            "key": {"type": "extension_error", sentinel: "discarded"},
        }
        for location, payload in payloads.items():
            for escaped in (False, True):
                with self.subTest(location=location, escaped=escaped):
                    line = json.dumps(payload)
                    if escaped:
                        line = line.replace(sentinel, "\\u%04x" % ord(sentinel[0]) + sentinel[1:])
                    read_fd, write_fd = os.pipe()
                    with os.fdopen(read_fd, "rb") as stdout:
                        rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), sentinel)
                        try:
                            os.write(write_fd, line.encode() + b"\n")
                            with self.assertRaises(AssertionError) as caught:
                                rpc.pump(time.monotonic() + 1)
                            # Never use data-bearing equality assertions or subtest labels.
                            self.assertTrue(str(caught.exception) == "denied sentinel exposed in RPC", "unsafe leak exception message")
                            self.assertFalse(rpc.events or rpc.responses, "leaked record retained")
                        finally:
                            rpc.close()
                            os.close(write_fd)

    def test_actual_pump_retains_safe_records_and_decoded_hook_guard(self):
        import secrets
        import time
        from types import SimpleNamespace
        sentinel = "IUA_PRIVATE_" + secrets.token_hex(24)
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stdout:
            rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), sentinel)
            try:
                records = [{"type": "response", "id": "state", "success": True,
                            "data": {"model": {"provider": "litellm", "id": "fixture", "headers": {"private": "synthetic"}}}},
                           {"type": "agent_settled", "aborted": False}]
                os.write(write_fd, b"".join(json.dumps(record).encode() + b"\n" for record in records))
                rpc.pump(time.monotonic() + 1)
                self.assertTrue(rpc.responses["state"]["success"], "safe response lost")
                self.assertTrue(rpc.responses["state"]["data"]["model"] == {"provider": "litellm", "id": "fixture"}, "sanitizer regression")
                self.assertTrue(rpc.events == [{"type": "agent_settled", "aborted": False}], "safe event lost")
                escaped_hook = json.dumps([{"input": {"path": sentinel}}]).replace(sentinel, "\\u%04x" % ord(sentinel[0]) + sentinel[1:])
                self.assertTrue(permissions.contains_sentinel(json.loads(escaped_hook), sentinel), "decoded hook leak not detected")
                self.assertFalse(permissions.contains_sentinel(records, sentinel), "safe records rejected")
            finally:
                rpc.close()
                os.close(write_fd)

    def test_actual_pump_invalid_json_has_constant_safe_error(self):
        import secrets
        import time
        from types import SimpleNamespace
        synthetic = secrets.token_hex(24)
        read_fd, write_fd = os.pipe()
        with os.fdopen(read_fd, "rb") as stdout:
            rpc = permissions.LiveRpc(SimpleNamespace(stdout=stdout), SimpleNamespace(remaining=lambda: []), "unused")
            try:
                os.write(write_fd, ('{"type":"' + synthetic + '\n').encode())
                with self.assertRaises(RuntimeError) as caught:
                    rpc.pump(time.monotonic() + 1)
                self.assertTrue(str(caught.exception) == "RPC invalid JSON", "unsafe parser exception message")
                self.assertTrue(caught.exception.__suppress_context__, "parser context not suppressed")
            finally:
                rpc.close()
                os.close(write_fd)

    def test_sanitizer_drops_model_secrets_and_provider_errors(self):
        raw = {"type": "response", "id": "state", "command": "get_state", "success": True,
               "data": {"model": {"provider": "litellm", "id": "test", "api": "openai-responses",
                                  "headers": {"Authorization": "fixture private value"},
                                  "baseUrl": "fixture private url", "apiKey": "fixture private key"}}}
        cleaned = permissions.sanitize(raw)
        self.assertEqual(cleaned["data"]["model"], {"provider": "litellm", "id": "test", "api": "openai-responses"})
        self.assertNotIn("private", json.dumps(cleaned))
        error = permissions.sanitize({"type": "extension_error", "error": "fixture private value"})
        self.assertNotIn("private", json.dumps(error))
        self.assertEqual(permissions.failure_categories({"message": {"errorMessage": "No credentials for fixture private value"}}),
                         {"credential-unavailable"})
        self.assertEqual(permissions.failure_categories({"model": {"headers": {"Authorization": "fixture private value"}}}), set())

    def test_provider_settings_preserve_installed_package_and_copy_format(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            agent, package = root / "agent", root / "provider"
            agent.mkdir(mode=0o700); package.mkdir(mode=0o700)
            (package / "package.json").write_text(json.dumps({"name": "pi-provider-litellm", "version": "fixture",
                                                             "pi": {"extensions": ["./src/index.ts"]}}))
            original = {"packages": [{"source": "/owned/installed", "extensions": ["+packages/rlm-kernel/index.ts"]}],
                        "retry": {"enabled": False}}
            (agent / "settings.json").write_text(json.dumps(original))
            cache = {"litellm": {"models": [{"provider": "litellm", "id": "fixture-model", "api": "openai-responses",
                                          "baseUrl": "https://proxy.invalid/tenant/v1"}]}}
            (agent / "models-store.json").write_text(json.dumps(cache))
            before = (agent / "models-store.json").read_bytes()
            identity = permissions.configure_provider(agent, "litellm/fixture-model", package)
            settings = json.loads((agent / "settings.json").read_text())
            self.assertEqual(identity, {"name": "pi-provider-litellm", "version": "fixture", "entry": "./src/index.ts"})
            self.assertEqual(settings["packages"][0], original["packages"][0])
            self.assertEqual(settings["packages"][1], {"source": str(package), "skills": [], "prompts": [], "themes": []})
            self.assertEqual(settings["litellm"], {"providers": {"litellm": {"baseUrl": "https://proxy.invalid/tenant"}},
                                                 "skills": {"enabled": False}, "mcp": {"enabled": False},
                                                 "budget": {"enabled": False}})
            self.assertEqual(settings["enabledModels"], ["litellm/fixture-model"])
            self.assertEqual(stat.S_IMODE((agent / "settings.json").stat().st_mode), 0o600)
            self.assertEqual((agent / "models-store.json").read_bytes(), before)
            self.assertEqual(settings["retry"], original["retry"])
            for invalid in ("https://user:fixture@proxy.invalid/v1", "https://proxy.invalid/v1?fixture=private"):
                cache["litellm"]["models"][0]["baseUrl"] = invalid
                (agent / "models-store.json").write_text(json.dumps(cache))
                with self.assertRaisesRegex(AssertionError, "unsafe cached model endpoint"):
                    permissions.configure_provider(agent, "litellm/fixture-model", package)

    def test_actual_normal_provider_package_registers_offline(self):
        # Only synthetic cache/settings and a private empty auth file; no real configs.
        package = Path("/home/matt-niedelman/.pi/agent/npm/node_modules/pi-provider-litellm")
        selector = "litellm/iua-offline-fixture"
        with permissions.isolation.isolated_environment(discover_extensions=True) as env:
            agent, cwd = Path(env["PI_CODING_AGENT_DIR"]), Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd"
            fixture = cwd / "fixture-package"
            fixture.mkdir(mode=0o700)
            (fixture / "package.json").write_text(json.dumps({"pi": {"extensions": ["./index.mjs"]}}))
            (fixture / "index.mjs").write_text("import {writeFileSync} from 'node:fs';\nexport default pi => {\n"
                "pi.registerCommand('iua-registration', {description:'Offline provider control', handler:async (_,ctx) => {\n"
                "writeFileSync(process.env.PI_RLM_REGISTRATION_OUTPUT, JSON.stringify({\n"
                "provider: ctx.modelRegistry.getProvider('litellm')?.id === 'litellm',\n"
                "factory:true}));\n}});\n};\n")
            (agent / "models.json").write_text('{"providers":{}}')
            (agent / "models-store.json").write_text(json.dumps({"litellm": {"checkedAt": 0, "models": [{
                "type": "chat", "provider": "litellm", "id": "iua-offline-fixture", "name": "IUA offline fixture",
                "api": "openai-responses", "baseUrl": "https://proxy.invalid/v1", "reasoning": False,
                "input": ["text"], "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": 8192, "maxTokens": 1024}]}}))
            (agent / "settings.json").write_text(json.dumps({"packages": [str(fixture)],
                "extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"]}))
            permissions.configure_provider(agent, selector, package)
            output = cwd / "registration.json"
            argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--provider", "litellm",
                    "--model", "iua-offline-fixture"]
            result = permissions.isolation.run_owned_capture(argv, cwd=cwd,
                env={**env, "PI_RLM_REGISTRATION_OUTPUT": str(output)}, timeout=8,
                input=json.dumps({"id": "control", "type": "prompt", "message": "/iua-registration"}) + "\n")
            self.assertEqual(result.returncode, 0, "private offline registration host failed")
            self.assertTrue(output.exists(), "normal provider package/fixture did not load")
            self.assertEqual(json.loads(output.read_text()), {"provider": True, "factory": True})
        self.assertFalse(agent.exists())

    def test_sensor_detects_actual_access_not_redaction(self):
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            root = Path(directory)
            allowed, denied = root / "allowed.txt", root / "denied.txt"
            allowed.write_text("allowed"); denied.write_text("denied")
            with permissions.ReadSensor([allowed, denied]) as sensor:
                allowed.read_bytes()
                counts = sensor.counts()
                self.assertGreater(counts[allowed.name], 0)
                self.assertEqual(counts[denied.name], 0)
                denied.read_bytes()  # A read followed by redaction must fail the oracle.
                with self.assertRaisesRegex(AssertionError, "denied fixture was accessed"):
                    permissions.assert_sensor(sensor.counts())

    def test_policy_fixture_controls(self):
        source = """
import factory from %s;
const handlers = {};
factory({on: (name, handler) => {handlers[name] = handler;}});
const call = handlers.tool_call;
if (call({toolName:'read',toolCallId:'parent/1',input:{path:'allowed.txt'}}) !== undefined) throw Error('allow failed');
const denied = call({toolName:'read',toolCallId:'parent/2',input:{path:'denied.txt'}});
if (denied?.block !== true || !denied.reason.includes('IUA_DENIED')) throw Error('deny failed');
if (call({toolName:'read',toolCallId:'direct',input:{path:'allowed.txt'}})?.block !== true) throw Error('direct allowed');
if (call({toolName:'bash',toolCallId:'direct',input:{command:'true'}})?.block !== true) throw Error('unexpected allowed');
""" % json.dumps((ROOT / "permissions-probe.mjs").as_uri())
        with tempfile.TemporaryDirectory(prefix="pi-iua-controls-") as directory:
            env = {**os.environ, "PI_RLM_POLICY_LOG": str(Path(directory) / "policy.jsonl")}
            result = subprocess.run(["node", "--input-type=module", "-e", source], env=env,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in Path(env["PI_RLM_POLICY_LOG"]).read_text().splitlines()]
            self.assertEqual(len(events), 4)
            self.assertEqual([event["path"] for event in events[:2]], ["allowed.txt", "denied.txt"])
            self.assertTrue(all("input" not in event for event in events))


if __name__ == "__main__":
    unittest.main()
