#!/usr/bin/env python3
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("permissions_replay", ROOT / "permissions_replay.py")
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


class AbortEvidenceTests(unittest.TestCase):
    def test_probe_publishes_readiness_only_after_abort_listener_is_bound(self):
        listener = replay.ABORT_PROBE.index('signal.addEventListener("abort"')
        ready = replay.ABORT_PROBE.index('"nested-start\\n"')
        self.assertLess(listener, ready)

    def abort_events(self, nested_end, *, parent_first=False):
        parent = "call_" + "a" * 32
        nested = parent + "/1"
        nested_event = {"type": "tool_execution_end", "toolName": "wait_signal",
                        "toolCallId": nested, **nested_end}
        parent_event = {"type": "tool_execution_end", "toolName": "python", "toolCallId": parent,
                        "isError": True,
                        "result": {"content": [{"type": "text", "text": "aborted"}]}}
        ends = [parent_event, nested_event] if parent_first else [nested_event, parent_event]
        return parent, [
            {"type": "tool_execution_start", "toolName": "python", "toolCallId": parent},
            {"type": "tool_execution_start", "toolName": "wait_signal", "toolCallId": nested,
             "parentToolCallId": parent},
            *ends,
            {"type": "agent_settled", "aborted": True},
        ]

    def test_accepts_nested_then_parent_ends(self):
        parent, events = self.abort_events({
            "isError": True, "result": {"content": [{"type": "text", "text": "This operation was aborted"}]}})
        replay.assert_abort_events(events, parent)

    def test_accepts_parent_then_nested_ends(self):
        parent, events = self.abort_events({
            "isError": True, "result": {"content": [{"type": "text", "text": "This operation was aborted"}]}},
            parent_first=True)
        replay.assert_abort_events(events, parent)

    def test_rejects_duplicate_end_id(self):
        parent, events = self.abort_events({
            "isError": True, "result": {"content": [{"type": "text", "text": "aborted"}]}})
        events.insert(-1, dict(events[2]))
        with self.assertRaisesRegex(AssertionError, "abort stage end-events"):
            replay.assert_abort_events(events, parent)

    def test_rejects_missing_end_id(self):
        parent, events = self.abort_events({
            "isError": True, "result": {"content": [{"type": "text", "text": "aborted"}]}})
        del events[2]
        with self.assertRaisesRegex(AssertionError, "abort stage end-events"):
            replay.assert_abort_events(events, parent)

    def test_rejects_successful_post_signal_nested_result(self):
        parent, events = self.abort_events({
            "isError": False, "result": {"content": [{"type": "text", "text": "late-result"}],
                                         "details": {"late": True}}})
        with self.assertRaisesRegex(AssertionError, "abort stage nested-result"):
            replay.assert_abort_events(events, parent)

    def test_abort_stage_binds_loop_callbacks_and_names_failure(self):
        marker = object()
        self.assertIs(replay.abort_stage("ready", lambda: marker), marker)
        with self.assertRaisesRegex(AssertionError, "abort stage ready: TimeoutError: opaque detail") as raised:
            replay.abort_stage("ready", lambda: (_ for _ in ()).throw(TimeoutError("opaque detail")))
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)

    def test_each_abort_stage_failure_names_its_stage(self):
        with self.assertRaisesRegex(AssertionError, "abort stage event-shape"):
            replay.assert_abort_events([], "call_" + "a" * 32)

    def test_final_drain_rejects_late_end_after_settled(self):
        parent, events = self.abort_events({
            "isError": True, "result": {"content": [{"type": "text", "text": "aborted"}]}})

        class Stdin:
            closed = False

            def close(self):
                self.closed = True

        class Process:
            stdin = Stdin()
            returncode = None

            def wait(self, timeout):
                self.returncode = 0
                return 0

        class Rpc:
            eof = False

            def __init__(self):
                self.events = events

            def pump(self, deadline):
                self.events.append({"type": "tool_execution_end", "toolCallId": parent + "/late",
                                    "isError": False, "result": {"content": [{"type": "text",
                                                                               "text": "late-result"}]}})
                self.eof = True

        class Owned:
            def remaining(self):
                return []

        with self.assertRaises(AssertionError):
            replay.final_abort_events(Process(), Rpc(), Owned(), parent)


class SotVerdictTests(unittest.TestCase):
    def report(self):
        return {"checks": {**{name: "PASS" for name in replay.SOT_CHECKS},
                           "sensor": "PASS", "sensor_monotonic": "PASS"}}

    def test_sensor_only_timing_failure_does_not_change_sot_verdict(self):
        report = self.report()
        self.assertTrue(replay.sot_verdict(report))
        report["checks"]["sensor"] = "FAIL"
        report["checks"]["sensor_monotonic"] = "FAIL"
        self.assertTrue(replay.sot_verdict(report))

    def test_ownership_failures_reject_sot_verdict(self):
        for check in ("hook_results", "nested_ids"):
            with self.subTest(check=check):
                report = self.report()
                report["checks"][check] = "FAIL"
                self.assertFalse(replay.sot_verdict(report))

    def test_missing_ownership_evidence_rejects_sot_verdict(self):
        report = self.report()
        del report["checks"]["hook_calls"]
        self.assertFalse(replay.sot_verdict(report))


class ReplayInventoryTests(unittest.TestCase):
    def test_default_remains_stream_and_permission_cases_without_abort(self):
        self.assertEqual(replay.inventory(sot=False), (True, replay.CASES, False))

    def test_sot_uses_ownership_only_inventory(self):
        self.assertEqual(replay.inventory(sot=True), (False, ("good",), True))


class PreflightTests(unittest.TestCase):
    def test_controlled_failure_is_exit69_before_replay_and_cleans_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            cache = base / "empty-cache"
            cache.mkdir()
            attempt = base / "attempt"
            disposable = base / "disposable"
            fake = base / "uv"
            fake.write_text("#!/bin/sh\nprintf '%s' \"$UV_PROJECT_ENVIRONMENT\" > \"$PI_RLM_UV_ATTEMPT\"\nexit 7\n")
            fake.chmod(0o700)
            with self.assertRaises(replay.PreflightUnavailable) as raised:
                replay.jupyter_preflight(cache, executable=fake, root=disposable,
                    extra_env={"PI_RLM_UV_ATTEMPT": str(attempt)})
            self.assertEqual(raised.exception.exit_code, 69)
            self.assertEqual(str(raised.exception), replay.PREFLIGHT_BLOCKED)
            self.assertEqual(attempt.read_text(), str(disposable / "environment"))
            self.assertFalse(disposable.exists())


if __name__ == "__main__":
    unittest.main()
