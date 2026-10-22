#!/usr/bin/env python3
"""Opt-in installed real-pi nested permission check (Linux/pi 1.1.0)."""
from contextlib import contextmanager
import ctypes
import importlib.util
import json
import os
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

    def pump(self, deadline):
        self.owned.remaining()
        if self.eof:
            raise RuntimeError("RPC EOF")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RPC wall deadline exceeded")
        for key, _ in self.selector.select(min(remaining, .02)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.eof = True
                raise RuntimeError("RPC EOF")
            self.total_bytes += len(chunk)
            if self.total_bytes > 4 * 1024 * 1024:
                raise RuntimeError("RPC aggregate limit exceeded")
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                assert self.sentinel not in line, "denied sentinel exposed in RPC"
                raw = json.loads(line)
                self.failure_categories.update(failure_categories(raw))
                message = sanitize(raw)
                if message.get("type") == "response":
                    self.responses[message.get("id")] = message
                else:
                    self.events.append(message)
            if len(self.buffer) > 1024 * 1024 or len(self.responses) > 32 or len(self.events) > 4096:
                raise RuntimeError("RPC protocol buffer limit exceeded")


def verify(rpc, policy, counts, selector):
    """Conjunctive oracle: actual args/details, real hooks/IDs and inode access."""
    assert_sensor(counts)
    settled = [e for e in rpc.events if e["type"] == "agent_settled"]
    assert len(settled) == 1 and settled[0]["aborted"] is False, "run did not settle successfully"
    starts = [e for e in rpc.events if e["type"] == "tool_execution_start"]
    python = [e for e in starts if e["toolName"] == "python"]
    assert len(python) == 1, "expected exactly one actual Python call"
    parent = python[0]["toolCallId"]
    args = python[0]["args"]
    assert args.get("code") == CELL and args.get("operation", "execute") == "execute", "actual cell mismatch"
    assert set(args) <= {"code", "operation"}, "unexpected Python arguments"
    nested = [e for e in starts if e["toolName"] == "read"]
    assert len(starts) == 3 and len(nested) == 2, "unexpected executed tools"
    ids = [parent + "/1", parent + "/2"]
    paths = [{"path": "allowed.txt"}, {"path": "denied.txt"}]
    assert [e["toolCallId"] for e in nested] == ids, "pi-owned nested ID correlation failed"
    assert [e["args"] for e in nested] == paths, "actual nested arguments mismatch"
    assert all(e.get("parentToolCallId") == parent for e in nested), "execution parent correlation failed"
    ends = [e for e in rpc.events if e["type"] == "tool_execution_end"]
    by_id = {e["toolCallId"]: e for e in ends}
    assert len(ends) == 3 and set(by_id) == {parent, *ids}, "execution end correlation failed"
    assert by_id[ids[0]]["isError"] is False and by_id[ids[1]]["isError"] is True, "nested status mismatch"
    assert "IUA_ALLOWED" in json.dumps(by_id[ids[0]]["result"]), "allowed content missing"
    assert "IUA_DENIED" in json.dumps(by_id[ids[1]]["result"]), "meaningful denied result missing"
    actual = by_id[parent]
    assert actual["isError"] is False, "Python failed"
    assert actual["result"]["details"] == {"result": EXPECTED, "output": "", "truncated": False}, "actual Python details mismatch"
    calls = [e for e in policy if e["type"] == "tool_call"]
    assert len(calls) == 3, "hook call counters mismatch"
    assert [(e["toolName"], e["toolCallId"]) for e in calls] == [("python", parent), ("read", ids[0]), ("read", ids[1])], "hook IDs/names mismatch"
    assert calls[0]["input"] == args and [e["input"] for e in calls[1:]] == paths, "hook arguments mismatch"
    results = [e for e in policy if e["type"] == "tool_result"]
    # Pi emits tool_result only for executed tools, not policy-blocked calls.
    assert [(e["toolName"], e["toolCallId"], e["isError"]) for e in results] == [("read", ids[0], False), ("python", parent, False)], "hook result counters mismatch"
    assistants = [e["message"] for e in rpc.events if e["type"] == "message_end" and e["message"]["role"] == "assistant"]
    assert assistants and all(m["provider"] + "/" + m["model"] == selector for m in assistants), "model substitution"
    assert all(m["stopReason"] not in ("error", "aborted", "length") for m in assistants), "model failed"
    usage = {k: sum(m["usage"].get(k, 0) for m in assistants) for k in ("input", "output", "cacheRead", "cacheWrite", "totalTokens")}
    assert usage["totalTokens"] > 0, "missing model usage"
    return {"model": selector, "assistantMessages": len(assistants), "usage": usage,
            "policyCalls": len(calls), "policyResults": len(results), "executionStarts": len(starts),
            "executionEnds": len(ends), "sensor": counts, "pythonDetails": actual["result"]["details"]}


def live_check(env, cwd, installed, selector):
    provider, model_id = selector.split("/", 1)
    assert provider == "litellm" and model_id, "selected configured LiteLLM model required"
    allowed, denied = cwd / "allowed.txt", cwd / "denied.txt"
    allowed.write_text("IUA_ALLOWED\n")
    sentinel = "IUA_PRIVATE_" + secrets.token_hex(24)
    denied.write_text(sentinel + "\n")
    for path in (allowed, denied):
        path.chmod(0o600)
    policy = cwd / "policy.jsonl"
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--tools", "python,read",
            "--provider", provider, "--model", model_id, "--thinking", "off",
            "--extension", str(ROOT / "permissions-probe.mjs")]
    # Explicit opt-in exact existing provider reference; no provider source copy.
    if env.get("PI_RLM_COMPAT_EXTENSION"):
        argv.extend(["--extension", env["PI_RLM_COMPAT_EXTENSION"]])
    with tempfile.TemporaryFile() as stderr, ReadSensor([allowed, denied]) as sensor:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "PI_RLM_POLICY_LOG": str(policy)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        owned = isolation.OwnedProcesses(proc)
        rpc = LiveRpc(proc, owned, sentinel)
        try:
            state = rpc.request("state", "get_state")
            assert state["success"] is True, "state request failed"
            assert state["data"]["model"]["provider"] + "/" + state["data"]["model"]["id"] == selector, "selected model mismatch"
            prompt = "Use the python tool exactly once with this EXACT code (preserve all characters and line breaks), then reply DONE. Do not call any other tool directly or retry the denied read.\n\n" + CELL
            response = rpc.request("permissions", "prompt", message=prompt)
            assert response["success"] is True and response["data"]["disposition"] == "started", "prompt not started"
            integration.wait_until(lambda: any(e["type"] == "agent_settled" for e in rpc.events), rpc, owned, timeout=55)
            summary = verify(rpc, [json.loads(line) for line in policy.read_text().splitlines()], sensor.counts(), selector)
            # Installed sidecar argv, not a copied factory or original index loader.
            sidecars = []
            for pid in owned.remaining():
                try:
                    argv_bytes = Path(f"/proc/{pid}/cmdline").read_bytes()
                except FileNotFoundError:
                    continue
                if str(installed / "packages/rlm-kernel/python/sidecar.py").encode() in argv_bytes:
                    sidecars.append(pid)
            assert len(sidecars) >= 1, "installed sidecar provenance missing"
            proc.stdin.close()
            proc.wait(timeout=5)
            assert proc.returncode == 0, "pi orderly shutdown failed"
            deadline = time.monotonic() + 3
            while owned.remaining() and time.monotonic() < deadline:
                time.sleep(.01)
            assert not owned.remaining(), "owned children survived before backup"
            stderr.seek(0)
            assert sentinel.encode() not in stderr.read(), "denied sentinel exposed in stderr"
            assert sentinel not in policy.read_text(), "denied sentinel exposed in hook log"
            assert_sensor(sensor.counts())
            print("permissions live PASS: " + json.dumps({**summary, "installedSidecars": len(sidecars),
                "observedOwnedPIDs": len(owned.observed), "survivorsBeforeBackup": 0}, sort_keys=True), flush=True)
        except BaseException:
            stderr.seek(0)
            diagnostic = stderr.read().decode(errors="replace")
            # Only allowlisted diagnostic classifications, never raw provider stderr.
            categories = sorted(failure_categories(diagnostic) | rpc.failure_categories)
            print("permissions safe failure context: " + json.dumps({"exit": proc.poll(),
                "categories": categories, "rpcEventTypes": sorted({e["type"] for e in rpc.events}),
                "sensor": sensor.counts()}), file=sys.stderr, flush=True)
            raise
        finally:
            owned.cleanup()
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
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm-kernel/index.ts"], "skills": [], "prompts": [], "themes": []}]
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
