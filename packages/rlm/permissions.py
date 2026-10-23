#!/usr/bin/env python3
"""Opt-in installed real-pi nested permission check (Linux/pi 1.1.0)."""
from contextlib import contextmanager
import ctypes
import importlib.util
import json
import os
import re
from pathlib import Path
import secrets
import stat
import struct
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("installation", ROOT / "install-check.py")
installation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installation)
integration = installation.integration
isolation = integration.isolation
MODEL_FILES = ("models.json", "models-store.json")
CELL = "allowed = pi.read(path='allowed.txt')\ndenied = pi.read(path='denied.txt')\n{'allowed': 'IUA_ALLOWED' in allowed['content'] and not allowed['is_error'], 'denied_is_error': denied['is_error'], 'denial_marker': 'IUA_DENIED' in denied['content']}"
EXPECTED = {"allowed": True, "denied_is_error": True, "denial_marker": True}


@contextmanager
def private_models(source, target):
    """Copy only the two explicitly authorized configs, never auth/settings."""
    created = []
    try:
        assert stat.S_IMODE(target.stat().st_mode) == 0o700, "private agent mode required"
        for name in MODEL_FILES:
            path = target / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(fd, "wb") as output:
                output.write((source / name).read_bytes())
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, "private model copy mode required"
        yield
    finally:
        for path in created:
            path.unlink(missing_ok=True)
        assert not any(path.exists() for path in created), "private model copies survived"


@contextmanager
def private_litellm_auth(source, target):
    """Read-only source; select only litellm into an exclusive private SDK copy."""
    original = None
    created = False
    path = target / "auth.json"
    try:
        try:
            mode = target.lstat()
            if (not stat.S_ISDIR(mode.st_mode) or stat.S_IMODE(mode.st_mode) != 0o700
                    or mode.st_uid != os.getuid() or source.resolve() == target.resolve()):
                raise ValueError("private owner-only agent required")
            original = (source / "auth.json").read_bytes()
            # Parsing the whole JSON is unavoidable; never inspect/serialize other entries.
            selected = json.loads(original)["litellm"]
            if not isinstance(selected, dict):
                raise ValueError("selected credential missing")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, "w") as output:
                json.dump({"litellm": selected}, output)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ValueError("private copy mode required")
        except Exception:
            raise RuntimeError("private LiteLLM auth setup failed") from None
        yield
    finally:
        # SDK refresh can replace this copy, but never receives the source path.
        try:
            if created:
                path.unlink(missing_ok=True)
        except OSError:
            raise RuntimeError("private LiteLLM auth cleanup failed") from None
        finally:
            if original is not None:
                try:
                    unchanged = (source / "auth.json").read_bytes() == original
                except OSError:
                    unchanged = False
                if not unchanged:
                    raise RuntimeError("source auth changed during private check") from None


def configure_provider(agent, selector, package):
    """Load the existing normal provider package, using only the private cache root."""
    provider, _, model_id = selector.partition("/")
    assert provider == "litellm" and model_id, "selected LiteLLM model required"
    manifest = json.loads((package / "package.json").read_text())
    assert manifest.get("name") == "pi-provider-litellm", "wrong normal provider package"
    assert manifest.get("pi", {}).get("extensions") == ["./src/index.ts"], "wrong normal provider entry"
    cache = json.loads((agent / "models-store.json").read_text())
    selected = [m for m in cache.get(provider, {}).get("models", []) if m.get("id") == model_id]
    assert len(selected) == 1, "selected cached model missing or ambiguous"
    model = selected[0]
    assert model.get("provider") == provider and model.get("api") == "openai-responses", "wrong cached model transport"
    assert "headers" not in model and "apiKey" not in model, "unexpected cached credential fields"
    try:
        endpoint = urlsplit(model["baseUrl"])
        safe = endpoint.scheme == "https" and endpoint.hostname and not (
            endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
        endpoint.port  # Validate without reporting URL values.
    except (KeyError, TypeError, ValueError):
        safe = False
    assert safe and endpoint.path.rstrip("/").endswith("/v1"), "unsafe cached model endpoint"
    root = urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path.rstrip("/")[:-3], "", ""))
    settings = agent / "settings.json"
    configured = json.loads(settings.read_text())
    configured.setdefault("packages", []).append({"source": str(package), "skills": [], "prompts": [], "themes": []})
    configured["litellm"] = {"providers": {provider: {"baseUrl": root}},
        **{feature: {"enabled": False} for feature in ("skills", "mcp", "budget")}}
    configured["enabledModels"] = [selector]
    settings.chmod(0o600)
    settings.write_text(json.dumps(configured))
    return {"name": "pi-provider-litellm", "version": manifest["version"], "entry": "./src/index.ts"}


def failure_categories(value):
    """Allowlisted categories only; never retain provider error material."""
    if isinstance(value, dict):
        return set().union(*(failure_categories(v) for k, v in value.items()
                             if k in ("error", "errorMessage", "finalError") or isinstance(v, (dict, list))))
    if isinstance(value, list):
        return set().union(*(failure_categories(v) for v in value))
    if not isinstance(value, str):
        return set()
    return {name for name, terms in {
        "model-not-found": ("model not found", "no models available", "unknown provider"),
        "credential-unavailable": ("no api key", "api key not found", "missing api key", "authentication",
                                   "no credentials", "missing credentials"),
        "extension-load-failure": ("failed to load extension", "cannot find module"),
    }.items() if any(term in value.lower() for term in terms)}


def sanitize(value):
    """Discard sensitive provider state/errors before storing RPC observations."""
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("headers", "apiKey", "baseUrl", "env", "diagnostics", "responseId",
                   "thinkingSignature", "textSignature", "thoughtSignature", "deferred"):
            continue
        if key in ("error", "errorMessage", "finalError"):
            result[key] = "[diagnostic suppressed]"
        elif key == "model" and isinstance(item, dict):
            result[key] = {k: item[k] for k in ("provider", "id", "api") if k in item}
        else:
            result[key] = sanitize(item)
    return result


def contains_sentinel(value, sentinel):
    """Check decoded observations, including keys discarded by sanitization."""
    if isinstance(value, str):
        return sentinel in value
    if isinstance(value, dict):
        return any(contains_sentinel(k, sentinel) or contains_sentinel(v, sentinel)
                   for k, v in value.items())
    if isinstance(value, list):
        return any(contains_sentinel(item, sentinel) for item in value)
    return False


class ReadSensor:
    """Linux IN_ACCESS on actual fixture inodes, independent of tool results."""
    def __init__(self, paths):
        libc = ctypes.CDLL(None, use_errno=True)
        self.fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify setup failed")
        self.watches = {}
        self.totals = {path.name: 0 for path in paths}
        try:
            for path in paths:
                wd = libc.inotify_add_watch(self.fd, os.fsencode(path), 1)  # IN_ACCESS
                if wd < 0:
                    raise OSError(ctypes.get_errno(), "inotify watch failed")
                self.watches[wd] = path.name
        except BaseException:
            os.close(self.fd)
            raise

    def counts(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, size = struct.unpack_from("iIII", data, offset)
                offset += 16 + size
                assert not mask & 0x4000, "inotify queue overflow"
                assert wd in self.watches and mask & 1, "unexpected sensor event"
                self.totals[self.watches[wd]] += 1
        return dict(self.totals)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        os.close(self.fd)


def assert_sensor(counts):
    assert counts["allowed.txt"] > 0, "allowed baseline did not access fixture"
    assert counts["denied.txt"] == 0, "denied fixture was accessed"


class LiveRpc(integration.Rpc):
    def __init__(self, proc, owned, sentinel):
        super().__init__(proc, owned)
        self.events = []
        self.sentinel = sentinel.encode()
        self.total_bytes = 0
        self.failure_categories = set()
        self.privacy_ok = True
        self.protocol_ok = True

    def records(self):
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if self.sentinel in line:
                self.privacy_ok = False
                raise AssertionError("denied sentinel exposed in RPC")
            try:
                raw = json.loads(line)
            except (ValueError, UnicodeError):
                self.protocol_ok = False
                raise RuntimeError("RPC invalid JSON") from None
            if contains_sentinel(raw, self.sentinel.decode()):
                self.privacy_ok = False
                raise AssertionError("denied sentinel exposed in RPC")
            if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
                self.protocol_ok = False
                raise RuntimeError("RPC invalid record")
            if raw["type"] == "response" and (not isinstance(raw.get("id"), str)
                    or raw["id"] not in ("state", "permissions") or raw["id"] in self.responses):
                self.protocol_ok = False
                raise RuntimeError("RPC invalid response")
            self.failure_categories.update(failure_categories(raw))
            message = sanitize(raw)
            if message["type"] == "response":
                self.responses[message["id"]] = message
            else:
                self.events.append(message)
            if len(self.responses) > 32 or len(self.events) > 4096:
                self.protocol_ok = False
                raise RuntimeError("RPC protocol buffer limit exceeded")

    def pump(self, deadline):
        self.owned.remaining()
        # A rejected record is discarded; later complete records remain inspectable.
        if b"\n" in self.buffer:
            self.records()
            return
        if self.eof:
            raise RuntimeError("RPC EOF")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RPC wall deadline exceeded")
        for key, _ in self.selector.select(min(remaining, .02)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.eof = True
                if self.buffer:
                    self.protocol_ok = False
                    self.buffer = b""
                raise RuntimeError("RPC EOF")
            self.total_bytes += len(chunk)
            if self.total_bytes > 4 * 1024 * 1024:
                self.protocol_ok = False
                self.buffer = b""
                raise RuntimeError("RPC aggregate limit exceeded")
            self.buffer += chunk
            self.records()
            if len(self.buffer) > 1024 * 1024:
                self.protocol_ok = False
                self.buffer = b""
                raise RuntimeError("RPC protocol buffer limit exceeded")

    def request(self, request_id, request_type, timeout=10, **values):
        # Do not use the base request's data-bearing assertion message.
        self.proc.stdin.write(json.dumps({"id": request_id, "type": request_type, **values}).encode() + b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            self.pump(deadline)
        response = self.responses.pop(request_id)
        if response.get("command") != request_type:
            self.protocol_ok = False
            raise RuntimeError("RPC response command mismatch")
        return response


PHASES = ("startup", "beforePrompt", "afterSettled", "finalShutdown")
EVENT_TYPES = frozenset(("agent_start", "agent_end", "agent_settled", "turn_start", "turn_end",
    "message_start", "message_update", "message_end", "tool_execution_start", "tool_execution_update",
    "tool_execution_end", "queue_update", "entry_appended", "session_info_changed", "thinking_level_changed",
    "compaction_start", "compaction_end", "auto_retry_start", "auto_retry_end", "summarization_retry_scheduled",
    "summarization_retry_attempt_start", "summarization_retry_finished", "extension_error", "extension_ui_request"))
ID_PATTERN = r"call_[A-Za-z0-9]{16,64}(?:\|fc_(?:call_)?[A-Za-z0-9]{16,64})?(?:/[1-9][0-9]{0,5})?"


def safe_id(value):
    return value if isinstance(value, str) and re.fullmatch(ID_PATTERN, value) else None


def safe_error_type(error):
    name = type(error).__name__
    return name if name in ("AssertionError", "RuntimeError", "TimeoutError", "OSError",
        "FileNotFoundError", "PermissionError", "ValueError", "TypeError", "KeyError", "BrokenPipeError",
        "TimeoutExpired", "KeyboardInterrupt", "SystemExit") else "Error"


def assert_report(report):
    assert report["passed"], "permissions invariant failure"


def verify(rpc, policy, phases, selector, *, replay=False):
    """Collect independent, bounded outcomes; never return uncontrolled observations."""
    events = [e for e in rpc.events if isinstance(e, dict)]
    settled = [e for e in events if e.get("type") == "agent_settled"]
    starts = [e for e in events if e.get("type") == "tool_execution_start"]
    python = [e for e in starts if e.get("toolName") == "python"]
    actual_start = python[0] if len(python) == 1 else {}
    parent = actual_start.get("toolCallId") if isinstance(actual_start.get("toolCallId"), str) else None
    args = actual_start.get("args")
    python_args_match = (isinstance(args, dict) and args.get("code") == CELL
        and args.get("operation", "execute") == "execute" and set(args) <= {"code", "operation"})
    nested = [e for e in starts if e.get("toolName") == "read"]
    ids = [parent + "/1", parent + "/2"] if isinstance(parent, str) else [None, None]
    paths = [{"path": "allowed.txt"}, {"path": "denied.txt"}]
    ends = [e for e in events if e.get("type") == "tool_execution_end"]
    by_id = {e["toolCallId"]: e for e in ends if isinstance(e.get("toolCallId"), str)}
    allowed, denied, actual = (by_id.get(i, {}) for i in (*ids, parent))
    calls = [e for e in policy if isinstance(e, dict) and e.get("type") == "tool_call"]
    results = [e for e in policy if isinstance(e, dict) and e.get("type") == "tool_result"]
    assistants = [e["message"] for e in events if e.get("type") == "message_end"
        and isinstance(e.get("message"), dict) and e["message"].get("role") == "assistant"]
    usage = {k: sum(m.get("usage", {}).get(k, 0) for m in assistants
        if isinstance(m.get("usage"), dict) and type(m["usage"].get(k, 0)) in (int, float)
        and 0 <= m["usage"].get(k, 0) <= 1000000000)
        for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")}
    phase_counts = {name: {path: phases[name].get(path) for path in ("allowed.txt", "denied.txt")}
        for name in PHASES if isinstance(phases.get(name), dict)}
    counts_valid = (len(phase_counts) == len(PHASES) and all(type(n) is int and 0 <= n <= 1000000000
        for counts in phase_counts.values() for n in counts.values()))
    checks = {
        "sensor": counts_valid and all(c["denied.txt"] == 0 for c in phase_counts.values())
            and phase_counts["afterSettled"]["allowed.txt"] > 0 and phase_counts["finalShutdown"]["allowed.txt"] > 0,
        "sensor_monotonic": counts_valid and all(phase_counts[a][p] <= phase_counts[b][p]
            for a, b in zip(PHASES, PHASES[1:]) for p in ("allowed.txt", "denied.txt")),
        "settled": len(settled) == 1 and settled[0].get("aborted") is False,
        "python_call_count": len(python) == 1,
        "actual_python_arguments": python_args_match,
        "execution_counts": len(starts) == 3 and len(nested) == 2,
        "nested_ids": isinstance(parent, str) and [e.get("toolCallId") for e in nested] == ids,
        "nested_arguments": [e.get("args") for e in nested] == paths,
        "nested_parent": len(nested) == 2 and parent is not None and all(e.get("parentToolCallId") == parent for e in nested),
        "execution_ends": len(ends) == 3 and parent is not None and set(by_id) == {parent, *ids},
        "nested_status": allowed.get("isError") is False and denied.get("isError") is True,
        "allowed_content": "IUA_ALLOWED" in json.dumps(allowed.get("result")),
        "denial_reason": "IUA_DENIED" in json.dumps(denied.get("result")),
        "python_status": actual.get("isError") is False,
        "python_result_envelope": isinstance(actual.get("result"), dict) and actual["result"].get("details")
            == {"result": EXPECTED, "output": "", "truncated": False},
        "hook_calls": len(calls) == 3,
        "hook_ids": len(calls) == 3 and all(e.get("idMatches") is True for e in calls)
            and [(e.get("toolName"), e.get("toolCallId")) for e in calls] == [("python", parent), ("read", ids[0]), ("read", ids[1])],
        "hook_arguments": len(calls) == 3 and all(e.get("argumentMatches") is True for e in calls)
            and [e.get("path") for e in calls[1:]] == ["allowed.txt", "denied.txt"],
        "hook_denial_reason": [e.get("denialReason") for e in calls] == ["none", "none", "IUA_DENIED"],
        # Blocked tools do not emit tool_result; only actual executions do.
        "hook_results": all(e.get("idMatches") is True and e.get("argumentMatches") is True for e in results)
            and [(e.get("toolName"), e.get("toolCallId"), e.get("isError")) for e in results]
                == [("read", ids[0], False), ("python", parent, False)],
        "selected_model": bool(assistants) and all(m.get("provider", "") + "/" + m.get("model", "") == selector for m in assistants
            if isinstance(m.get("provider"), str) and isinstance(m.get("model"), str))
            and all(isinstance(m.get("provider"), str) and isinstance(m.get("model"), str) for m in assistants),
        "model_status": bool(assistants) and all(m.get("stopReason") in ("stop", "toolUse") for m in assistants),
        # Live proof still requires positive paid usage. Replay is a distinct,
        # fixed local transport with exactly two responses and no inference.
        ("replay_zero_inference" if replay else "model_usage"): (
            selector == "q1s-local-replay/fixed-python" and len(assistants) == 2
            and [m.get("stopReason") for m in assistants] == ["toolUse", "stop"]
            and all(isinstance(m.get("usage"), dict)
                and all(type(m["usage"].get(k)) is int and m["usage"][k] == 0 for k in usage)
                and isinstance(m["usage"].get("cost"), dict)
                and all(type(m["usage"]["cost"].get(k)) in (int, float) and m["usage"]["cost"][k] == 0
                    for k in ("input", "output", "cacheRead", "cacheWrite", "total")) for m in assistants)
            if replay else usage["totalTokens"] > 0),
        "rpc_privacy": rpc.privacy_ok,
        "rpc_protocol": rpc.protocol_ok,
        "rpc_event_types": all(isinstance(e.get("type"), str) and e["type"] in EVENT_TYPES for e in events),
    }
    # Only valid fixture IDs, booleans and exact path whitelist leave this function.
    observations = [{"type": e["type"], "toolName": e.get("toolName") if e.get("toolName") in ("python", "read") else "other",
        "idMatches": e.get("idMatches") is True, "argumentMatches": e.get("argumentMatches") is True,
        **({"toolCallId": safe_id(e.get("toolCallId"))} if safe_id(e.get("toolCallId")) else {}),
        **({"path": e["path"]} if e.get("path") in ("allowed.txt", "denied.txt") else {}),
        **({"denialReason": e["denialReason"] if e.get("denialReason") in ("none", "IUA_DENIED", "IUA_UNEXPECTED_TOOL") else "unknown"}
            if e["type"] == "tool_call" else {"isError": e.get("isError") if type(e.get("isError")) is bool else None})}
        for e in (calls + results)[:16]]
    safe_phases = {phase: {p: n if type(n) is int and 0 <= n <= 1000000000 else None for p, n in counts.items()}
        for phase, counts in phase_counts.items()}
    return {"passed": all(checks.values()), "checks": {name: "PASS" if ok else "FAIL" for name, ok in checks.items()},
        "model": selector, "assistantMessages": len(assistants), "usage": usage, "policyCalls": len(calls),
        "policyResults": len(results), "executionStarts": len(starts), "executionEnds": len(ends), "sensorPhases": safe_phases,
        "actualPythonArgumentsMatch": python_args_match, "nestedArgumentsMatch": checks["nested_arguments"],
        "hooks": observations, "unknownEventTypes": sum(not isinstance(e.get("type"), str) or e["type"] not in EVENT_TYPES for e in events)}


def read_policy(path, sentinel):
    """Discard leaked/malformed records, but inspect every available hook record."""
    events, privacy_ok, protocol_ok = [], True, True
    try:
        data = path.read_bytes()
    except OSError:
        return events, privacy_ok, False
    if len(data) > 1024 * 1024:
        return events, privacy_ok, False
    for line in data.split(b"\n"):
        if not line:
            continue
        if sentinel.encode() in line:
            privacy_ok = False
            continue
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeError):
            protocol_ok = False
            continue
        if contains_sentinel(raw, sentinel):
            privacy_ok = False
            continue
        if not isinstance(raw, dict) or raw.get("type") not in ("tool_call", "tool_result"):
            protocol_ok = False
            continue
        events.append(raw)
    return events, privacy_ok, protocol_ok


def collect_settled(rpc, timeout=55):
    """Continue only observation after a rejected record, never resend a prompt."""
    errors = set()
    deadline = time.monotonic() + timeout
    while not any(e.get("type") == "agent_settled" for e in rpc.events):
        try:
            rpc.pump(deadline)
        except (AssertionError, RuntimeError, TimeoutError) as error:
            errors.add(safe_error_type(error))
            if rpc.eof or time.monotonic() >= deadline or rpc.total_bytes > 4 * 1024 * 1024:
                break
    return errors


def live_check(env, cwd, installed, selector, *, replay=False):
    provider, model_id = selector.split("/", 1)
    assert (selector == "q1s-local-replay/fixed-python" if replay else provider == "litellm" and model_id), "wrong permission transport selector"
    allowed, denied = cwd / "allowed.txt", cwd / "denied.txt"
    allowed.write_text("IUA_ALLOWED\n")
    sentinel = "IUA_PRIVATE_" + secrets.token_hex(24)
    denied.write_text(sentinel + "\n")
    for path in (allowed, denied):
        path.chmod(0o600)
    policy = cwd / "policy.jsonl"
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--tools", "python,read",
            "--provider", provider, "--model", model_id, "--thinking", "off",
            "--extension", str(ROOT / ("permissions-replay-probe.ts" if replay else "permissions-probe.mjs"))]
    if env.get("PI_RLM_COMPAT_EXTENSION"):
        argv.extend(["--extension", env["PI_RLM_COMPAT_EXTENSION"]])
    with tempfile.TemporaryFile() as stderr, ReadSensor([allowed, denied]) as sensor:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "PI_RLM_POLICY_LOG": str(policy), "PI_RLM_EXPECTED_CELL": CELL},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        owned = isolation.OwnedProcesses(proc)
        rpc = LiveRpc(proc, owned, sentinel)
        phases, errors, guards = {}, set(), {}
        sidecars = []
        try:
            try:
                phases["startup"] = sensor.counts()
            except Exception as error:
                errors.add(safe_error_type(error))
            state = {}
            try:
                state = rpc.request("state", "get_state")
            except Exception as error:
                errors.add(safe_error_type(error))
            model = state.get("data", {}).get("model", {}) if isinstance(state.get("data"), dict) else {}
            guards["state_response"] = state.get("success") is True
            guards["state_model"] = isinstance(model, dict) and model.get("provider") == provider and model.get("id") == model_id
            try:
                phases["beforePrompt"] = sensor.counts()
            except Exception as error:
                errors.add(safe_error_type(error))
            guards["prompt_response"] = False
            if guards["state_response"] and guards["state_model"]:
                prompt = "Use the python tool exactly once with this EXACT code (preserve all characters and line breaks), then reply DONE. Do not call any other tool directly or retry the denied read.\n\n" + CELL
                try:
                    response = rpc.request("permissions", "prompt", message=prompt)
                    guards["prompt_response"] = response.get("success") is True and isinstance(response.get("data"), dict) and response["data"].get("disposition") == "started"
                except Exception as error:
                    errors.add(safe_error_type(error))
                errors.update(collect_settled(rpc, timeout=12 if replay else 55))
            try:
                phases["afterSettled"] = sensor.counts()
            except Exception as error:
                errors.add(safe_error_type(error))
            # Installed sidecar argv, not a copied factory or original index loader.
            for pid in owned.remaining():
                try:
                    argv_bytes = Path(f"/proc/{pid}/cmdline").read_bytes()
                except FileNotFoundError:
                    continue
                if str(installed / "packages/rlm/python/sidecar.py").encode() in argv_bytes:
                    sidecars.append(pid)
            guards["installed_sidecar"] = bool(sidecars)
            proc.stdin.close()
            deadline = time.monotonic() + 5
            # Drain final stdout while observing orderly shutdown, including rejected records.
            while not rpc.eof and time.monotonic() < deadline:
                try:
                    rpc.pump(deadline)
                except (AssertionError, RuntimeError, TimeoutError) as error:
                    if not rpc.eof:
                        errors.add(safe_error_type(error))
                    if rpc.total_bytes > 4 * 1024 * 1024:
                        break
            try:
                proc.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                errors.add(safe_error_type(error))
            guards["orderly_shutdown"] = proc.poll() == 0
            deadline = time.monotonic() + 3
            while owned.remaining() and time.monotonic() < deadline:
                time.sleep(.01)
            survivors = len(owned.remaining())
            guards["owned_before_backup"] = survivors == 0
            try:
                phases["finalShutdown"] = sensor.counts()
            except Exception as error:
                errors.add(safe_error_type(error))
            stderr.seek(0)
            diagnostic = stderr.read()
            guards["stderr_privacy"] = sentinel.encode() not in diagnostic
            categories = sorted(failure_categories(diagnostic.decode(errors="replace")) | rpc.failure_categories)
            policy_events, privacy_ok, protocol_ok = read_policy(policy, sentinel)
            guards["hook_privacy"] = privacy_ok
            guards["hook_protocol"] = protocol_ok
            guards["observation_errors"] = not errors
            report = verify(rpc, policy_events, phases, selector, replay=replay)
            report["checks"].update({name: "PASS" if ok else "FAIL" for name, ok in guards.items()})
            report.update({"passed": all(value == "PASS" for value in report["checks"].values()),
                "installedSidecars": len(sidecars), "observedOwnedPIDs": len(owned.observed),
                "survivorsBeforeBackup": survivors, "exit": proc.poll(), "categories": categories,
                "errorTypes": sorted(errors)})
            print("permissions live " + ("PASS: " if report["passed"] else "FAIL: ") + json.dumps(report, sort_keys=True), flush=True)
            assert_report(report)
        finally:
            try:
                owned.cleanup()
            finally:
                rpc.close()
                if not proc.stdin.closed:
                    proc.stdin.close()
                proc.stdout.close()


def worker(selector):
    env = dict(os.environ)
    cwd = Path.cwd()
    with isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-iua-source-") as directory:
        fixture = Path(directory)
        original, installed = fixture / "original", fixture / "installed"
        command = lambda *argv: installation.command(env, cwd, *argv)
        sha = command("git", "-C", installation.REPO, "rev-parse", "HEAD")
        command("git", "clone", "--no-hardlinks", "--no-checkout", installation.REPO, original)
        command("git", "-C", original, "checkout", "--detach", sha)
        command("git", "clone", "--no-hardlinks", original, installed)
        command("git", "-C", installed, "checkout", "--detach", sha)
        command("git", "-C", installed, "remote", "remove", "origin")
        installation.committed_source(env, cwd, installed, sha)
        original.rename(fixture / "unavailable-original")
        settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings.write_text(json.dumps({"extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"],
                                       "retry": {"enabled": False}, "compaction": {"enabled": False}, "cacheWarming": "off"}))
        installer = env["_PI_ISOLATED_EXECUTABLE"]
        command(installer, "install", installed)
        configured = json.loads(settings.read_text())
        packages = configured.get("packages", [])
        assert len(packages) == 1 and isinstance(packages[0], str), "actual installer declaration missing"
        assert (settings.parent / packages[0]).resolve() == installed, "wrong installed package"
        command(installer, "list")
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm/index.ts"], "skills": [], "prompts": [], "themes": []}]
        settings.write_text(json.dumps(configured))
        settings.chmod(0o600)
        identity = configure_provider(settings.parent, selector, Path(env["PI_RLM_PROVIDER_PACKAGE"]))
        print("permissions normal provider package: " + json.dumps(identity, sort_keys=True), flush=True)
        print(f"permissions source: {sha}; independent committed clone; private agent700/model600; original fixture unavailable", flush=True)
        live_check(env, cwd, installed, selector)
    assert not fixture.exists(), "owned source fixture survived"


def run(selector):
    provider, _, model_id = selector.partition("/")
    if provider != "litellm" or not model_id:
        raise RuntimeError("PI_RLM_TEST_MODEL must select the configured LiteLLM provider/model")
    source = Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
    compatibility = source / "extensions/litellm-bedrock-compat.ts"
    with isolation.isolated_environment(discover_extensions=True) as env:
        agent = Path(env["PI_CODING_AGENT_DIR"])
        env.update({"UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "PI_RLM_PROVIDER_PACKAGE": str(source / "npm/node_modules/pi-provider-litellm")})
        if compatibility.is_file():
            env["PI_RLM_COMPAT_EXTENSION"] = str(compatibility)
        with private_models(source, agent), private_litellm_auth(source, agent):
            result = isolation.run_owned_capture([sys.executable, str(ROOT / "e2e.py"), "--case", "permissions", "--_worker", "--model", selector],
                cwd=agent.parent / "cwd", env=env, timeout=75)
        # Worker never prints provider diagnostics, raw state/config or errors.
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        status = result.returncode
    assert not agent.exists(), "private fixture root survived"
    print("permissions source auth unchanged (in-memory byte comparison); private selected-only auth/model copies absent", flush=True)
    print("permissions private config/agent/cwd/session/state/source cleanup: absent", flush=True)
    return status
