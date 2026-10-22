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
            self.assertEqual([event["input"] for event in events[:2]], [{"path": "allowed.txt"}, {"path": "denied.txt"}])


if __name__ == "__main__":
    unittest.main()
