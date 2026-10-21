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
            self.assertEqual([event["input"] for event in events[:2]], [{"path": "allowed.txt"}, {"path": "denied.txt"}])


if __name__ == "__main__":
    unittest.main()
