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
                message = sanitize(json.loads(line))
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
    compatibility = Path.home() / ".pi/agent/extensions/litellm-bedrock-compat.ts"
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
        env.update({"UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
        if compatibility.is_file():
            env["PI_RLM_COMPAT_EXTENSION"] = str(compatibility)
        with private_models(source, agent):
            result = isolation.run_owned_capture([sys.executable, str(ROOT / "e2e.py"), "--case", "permissions", "--_worker", "--model", selector],
                cwd=agent.parent / "cwd", env=env, timeout=75)
        # Worker never prints provider diagnostics, raw state/config or errors.
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        status = result.returncode
    assert not agent.exists(), "private fixture root survived"
    print("permissions private config/agent/cwd/session/state/source cleanup: absent", flush=True)
    return status
