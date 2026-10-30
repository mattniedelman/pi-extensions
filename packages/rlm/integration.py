#!/usr/bin/env python3
"""Offline actual-index wiring and real-pi cleanup checks (Linux/pi 1.1.0)."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("isolation", ROOT / "isolated-check.py")
if spec is None or spec.loader is None:
    raise RuntimeError("failed to load isolated-check.py")
isolation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)
sensor_spec = importlib.util.spec_from_file_location("legacy_state_sensor", ROOT / "legacy_state_sensor.py")
if sensor_spec is None or sensor_spec.loader is None:
    raise RuntimeError("failed to load legacy_state_sensor.py")
legacy_sensor = importlib.util.module_from_spec(sensor_spec)
sensor_spec.loader.exec_module(legacy_sensor)


class Rpc:
    """Drain binary stdout on LF with real wall deadlines, never blocking readline."""
    def __init__(self, proc, owned):
        self.proc = proc
        self.owned = owned
        self.selector = selectors.DefaultSelector()
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.responses = {}
        self.events = []
        self.eof = False

    def pump(self, deadline):
        self.owned.remaining()  # reap owned adoptees while a command is still running
        if self.eof:
            raise RuntimeError(f"RPC EOF (exit={self.proc.poll()})")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RPC wall deadline exceeded")
        for key, _ in self.selector.select(min(remaining, .02)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.eof = True
                self.selector.unregister(key.fileobj)
                raise RuntimeError(f"RPC EOF (exit={self.proc.poll()})")
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                message = json.loads(line)
                if message.get("type") == "response":
                    self.responses[message.get("id")] = message
                else:
                    self.events.append(message)
            if len(self.buffer) > 1024 * 1024 or len(self.responses) > 256:
                raise RuntimeError("RPC protocol buffer limit exceeded")

    def request(self, request_id, request_type, timeout: float = 10, **values):
        try:
            self.proc.stdin.write(json.dumps({"id": request_id, "type": request_type, **values}).encode() + b"\n")
            self.proc.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError(f"RPC stdin closed (exit={self.proc.poll()})") from error
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            self.pump(deadline)
        response = self.responses.pop(request_id)
        assert response["command"] == request_type, response
        return response

    def close(self):
        self.selector.close()


def wait_until(predicate, rpc, owned, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        owned.remaining()
        rpc.pump(deadline)


def protocol_checks(env, cwd):
    # Silence, partial LF, unrelated response, Unicode separator, and EOF.
    fixtures = [
        ("import time; time.sleep(5)", TimeoutError),
        ("import os,time; os.write(1,b'{'); time.sleep(5)", TimeoutError),
        ("import os,time; os.write(1,b'{\"type\":\"response\",\"id\":\"other\"}\\n'); time.sleep(5)", TimeoutError),
        ("pass", RuntimeError),
    ]
    for source, expected in fixtures:
        proc = subprocess.Popen([sys.executable, "-c", source], cwd=cwd, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        started = time.monotonic()
        try:
            try:
                rpc.request("test", "get_state", timeout=.15)
            except expected:
                pass
            else:
                raise AssertionError("missing protocol failure")
            assert time.monotonic() - started < 1, "deadline was not effective"
        finally:
            owned.cleanup()
            rpc.close()
            proc.stdin.close()
            proc.stdout.close()
    source = "import os; os.write(1, '{\"type\":\"response\",\"id\":\"test\",\"command\":\"get_state\",\"text\":\"a\\u2028b\"}\\n'.encode()); import time; time.sleep(5)"
    proc = subprocess.Popen([sys.executable, "-c", source], cwd=cwd, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    assert proc.stdin is not None and proc.stdout is not None
    owned = isolation.OwnedProcesses(proc)
    rpc = Rpc(proc, owned)
    try:
        assert rpc.request("test", "get_state")["text"] == "a\u2028b"
    finally:
        owned.cleanup()
        rpc.close()
        proc.stdin.close()
        proc.stdout.close()
    print("RPC deadline/framing negatives: 5 passed", flush=True)


def pi_check(env, cwd, probe, output, mode, injected=False):
    command = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--no-extensions", "--extension", str(probe)]
    if not injected:
        command.extend(["--extension", str(ROOT / "index.ts")])
    output.unlink(missing_ok=True)
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env={**env, "PI_RLM_PROBE_OUTPUT": str(output)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        identities = {}
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", timeout=30 if injected else 10, message="/probe-rlm")["success"] is True
            wait_until(output.exists, rpc, owned, timeout=35 if injected else 20)
            result = json.loads(output.read_text())
            assert "error" not in result, result
            if injected:
                assert result["passed"] == 24 and result["wiringOnly"] is True, result  # frozen before mbl.2 implementation
                assert result["rendering"] == 10, result
            else:
                assert result["name"] == "python" and result["exposure"] == "model-only", result
                assert "pi.read" in result["description"], result
                wait_until(lambda: len(owned.remaining()) >= 4, rpc, owned)
                identities = dict(owned.seen)
                assert len(identities) >= 4, "real pi/uv/Python/Jupyter must be observed"
            if mode == "EOF":
                proc.stdin.close()
            elif mode == "TERM":
                proc.terminate()
            else:
                proc.kill()
            proc.wait(timeout=5)
            assert proc.returncode == {"EOF": 0, "TERM": 143, "KILL": -signal.SIGKILL}[mode], proc.returncode
            if mode != "KILL":
                deadline = time.monotonic() + 3
                while owned.remaining() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert not owned.remaining(), "orderly pi shutdown left owned children BEFORE backup"
            else:
                owned.cleanup()
                for pid, start in identities.items():
                    info = isolation.process_info(pid)
                    assert info is None or info[0] != start, (pid, start, info)
            summary = result if injected else {"name": result["name"], "exposure": result["exposure"]}
            print(f"real pi {mode}: {summary}, 0 survivors", flush=True)
        except BaseException:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()


RUNTIME_FILES = ("index.ts", "render.ts", "client.mjs", "bridge.mjs", "installed-generation.mjs",
                 "python/sidecar.py", "python/pyproject.toml", "python/uv.lock")


def independent_source(root):
    root = root.resolve(strict=True)
    original = ROOT.parents[1]
    assert not root.is_relative_to(original), "installed root must be independent of original worktree"
    for relative in ("package.json", *("packages/rlm/" + name for name in RUNTIME_FILES)):
        path = root / relative
        assert path.resolve(strict=True).is_relative_to(root), f"installed source escapes independent root: {relative}"
        assert not path.samefile(original / relative), f"installed source aliases original: {relative}"
    return root


def sha256(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wait_settled(rpc, offset, owned, timeout=20):
    deadline = time.monotonic() + timeout
    while not any(event.get("type") == "agent_settled" for event in rpc.events[offset:]):
        owned.remaining()
        rpc.pump(deadline)
    settled = [event for event in rpc.events[offset:] if event.get("type") == "agent_settled"]
    assert settled == [{"type": "agent_settled", "aborted": False}], settled
    return rpc.events[offset:]


def orderly_stop(proc, rpc, owned):
    proc.stdin.close()
    proc.wait(timeout=5)
    assert proc.returncode == 0, proc.returncode
    deadline = time.monotonic() + 3
    while owned.remaining() and time.monotonic() < deadline:
        time.sleep(.01)
    assert not owned.remaining(), "installed pi left owned descendants BEFORE backup"


def run_probe(env, cwd, installed, mode):
    source = installed / "packages/rlm/index.ts"
    fixture = cwd / ("installed-" + mode)
    fixture.mkdir()
    output = fixture / "result.json"
    case_env = {**env, "PI_RLM_PROBE_OUTPUT": str(output), "PI_RLM_PROBE_MODE": mode,
                "PI_RLM_ENV_DIR": str(fixture / "venv")}
    command = [env["PI_BIN"], "--mode", "rpc", "--no-session"]
    loader = fixture / "loader.ts"
    if mode == "automatic":
        loader.write_text(f"import probe from {json.dumps(str(ROOT / 'installed-probe.ts'))};\n" +
                          f"export default host => probe(host, undefined, {json.dumps(str(source))});\n")
        command.extend(["--extension", str(loader)])
    else:
        loader.write_text(f"import factory from {json.dumps(str(source))};\n" +
                          f"import probe from {json.dumps(str(ROOT / 'installed-probe.ts'))};\n" +
                          f"export default host => probe(host, factory, {json.dumps(str(source))});\n")
        command.extend(["--no-extensions", "--extension", str(loader)])
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=case_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            if mode == "automatic":
                offset = len(rpc.events)
                rpc.request("automatic-start", "prompt", message="PROCESS_AUTO\nUse Python exactly once, then reply DONE.")
                wait_settled(rpc, offset, owned)
            assert rpc.request("probe", "prompt", timeout=35, message="/probe-rlm")["success"] is True
            wait_until(output.exists, rpc, owned, timeout=40)
            result = json.loads(output.read_text())
            assert result.get("ready") is True and "error" not in result, result
            assert result["source"] == str(source.resolve()), result
            assert result["sourceIdentity"] == {"device": source.stat().st_dev, "inode": source.stat().st_ino}, result
            generation = result["generation"]
            sidecar = str(installed / "packages/rlm/python/sidecar.py")
            assert sum(sidecar in child["argv"] for child in generation) == 2, generation
            assert all(str(ROOT / "python/sidecar.py") not in child["argv"] for child in generation), generation
            if mode == "automatic":
                assert result["discovered"] == {"name": "python", "exposure": "model-only"}, result
                marker = "installed automatic direct-Jupyter generation: 1, 0 survivors"
            else:
                assert result["values"] == [42, 43, 44], result
                assert result["variables"] == 256 and result["reset"] == "Python workspace reset; previous live state was discarded", result
                assert result["reload"] == "Python workspace started fresh; state from an earlier process is not restored", result
                assert result["checkpoint"] == "Python checkpoints are unsupported; state lasts only for the live pi session.", result
                marker = "installed injected 42/43/error44/variables256/reset/reload/checkpoint unsupported, 0 survivors"
            identities = {(pid, start) for pid, start in owned.seen.items()}
            orderly_stop(proc, rpc, owned)
            print(marker, flush=True)
            return identities
        except BaseException:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()


def normalize_bootstrap_error(payload):
    """Classify only fields captured from the Error that rejected the installed tool call."""
    assert payload.get("name") == "Error", payload
    message = payload.get("message", "")
    assert isinstance(message, str), payload
    code = payload.get("code")
    fields = payload.get("fields", {})
    assert isinstance(fields, dict), payload
    cause = payload.get("cause")
    if code == "ENOENT" and "uv" in message:
        return {"code": code, "message": message}
    status = fields.get("status")
    if status is None:
        match = re.search(r"exited \((\d+)\)", message)
        status = int(match.group(1)) if match else None
    if status == 78 and "qwy bootstrap deliberately failed" in message:
        return {"status": status, "message": message}
    raise AssertionError(f"unrecognized caller-visible bootstrap error: {payload!r}; cause={cause!r}")


def bootstrap_check(env, cwd, installed, mode):
    source = installed / "packages/rlm/index.ts"
    fixture = cwd / ("bootstrap-" + mode)
    for name in ("bin", "venv"):
        (fixture / name).mkdir(parents=True, mode=0o700)
    node = shutil.which("node", path=env["PATH"])
    if node:
        (fixture / "bin/node").symlink_to(node)
    attempts = fixture / "uv-attempts"
    if mode == "process-exit78":
        uv = fixture / "bin/uv"
        uv.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\nPath({str(attempts)!r}).open('a').write('1\\n')\nsys.stderr.write('qwy bootstrap deliberately failed\\n')\nsys.exit(78)\n")
        uv.chmod(0o700)
    loader = fixture / "loader.ts"
    loader.write_text(f"import factory from {json.dumps(str(source))};\n" +
                      f"import probe from {json.dumps(str(ROOT / 'installed-probe.ts'))};\n" +
                      f"export default host => probe(host, factory, {json.dumps(str(source))});\n")
    output = fixture / "result.json"
    case_env = {**env, "PATH": str(fixture / "bin"), "PI_RLM_PROBE_OUTPUT": str(output),
                "PI_RLM_PROBE_MODE": "injected", "PI_RLM_BOOTSTRAP_EXPECTED": mode,
                "PI_RLM_ENV_DIR": str(fixture / "venv")}
    command = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--no-extensions", "--extension", str(loader)]
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=case_env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            rpc.request("state", "get_state")
            rpc.request("probe", "prompt", timeout=15, message="/probe-rlm")
            wait_until(output.exists, rpc, owned, timeout=18)
            result = json.loads(output.read_text())
            payload = result.get("bootstrapError")
            assert payload, "installed probe did not capture the caller-visible Error"
            actual = normalize_bootstrap_error(payload)
            message = actual.get("message")
            assert isinstance(message, str), actual
            if mode == "missing-executable":
                assert actual["code"] == "ENOENT" and "uv" in message, actual
                false_positive = {**payload, "code": None}
            else:
                assert actual["status"] == 78 and "qwy bootstrap deliberately failed" in message, actual
                false_positive = {**payload, "message": "qwy bootstrap deliberately failed", "fields": {}}
            try:
                normalize_bootstrap_error(false_positive)
            except AssertionError:
                pass
            else:
                raise AssertionError("bootstrap classifier accepted a payload missing its distinguishing caller-visible field")
            orderly_stop(proc, rpc, owned)
            if mode == "missing-executable":
                assert len(owned.observed) == 1, "missing uv launched an alternate child command"
            else:
                assert attempts.read_text().splitlines() == ["1"], "fake uv did not execute exactly once"
            print(f"bootstrap {mode}: caller-visible {json.dumps(actual, sort_keys=True)}, false-positive rejected, 0 survivors", flush=True)
        finally:
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()


def process_exit_check(env, cwd, installed):
    provider = ROOT / "installed-probe.ts"
    source = installed / "packages/rlm/index.ts"
    common = ["--mode", "rpc", "--tools", "python", "--provider", "rlm-installed-local",
              "--model", "fixed", "--thinking", "off", "--extension", str(provider)]
    session = None
    prior_pi = prior_kernel = None
    for label in ("A", "B"):
        command = [env["PI_BIN"], *common] + (["--session", session] if session else [])
        case_env = {**env, "PI_RLM_PROBE_MODE": "provider", "PI_RLM_ENV_DIR": str(cwd / ("process-" + label + "-venv"))}
        with tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(command, cwd=cwd, env=case_env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
            assert proc.stdin is not None and proc.stdout is not None
            owned = isolation.OwnedProcesses(proc)
            rpc = Rpc(proc, owned)
            try:
                state = rpc.request(label + "-state", "get_state")["data"]
                captured = Path(state["sessionFile"]).resolve()
                assert captured.is_relative_to(Path(env["PI_CODING_AGENT_SESSION_DIR"]).resolve())
                if session is None:
                    session = str(captured)
                    offset = len(rpc.events)
                    assert rpc.request("A-prompt", "prompt", message="PROCESS_A\nSet process_value using Python exactly once, then reply DONE.")["data"]["disposition"] == "started"
                    events = wait_settled(rpc, offset, owned)
                    entries = rpc.request("A-entries", "get_entries")["data"]["entries"]
                    messages = [entry.get("message", {}) for entry in entries if entry.get("type") == "message"]
                    assert any(message.get("role") == "user" for message in messages), entries
                    assistants = [message for message in messages if message.get("role") == "assistant"]
                    assert any(any(part.get("type") == "toolCall" and part.get("name") == "python" and part.get("arguments") == {"code": "process_value = 99\nprocess_value"} for part in message.get("content", [])) for message in assistants), assistants
                    assert any(event.get("type") == "tool_execution_end" and event.get("toolName") == "python" and event.get("isError") is False for event in events), events
                    assert any(any(part.get("type") == "text" and part.get("text") == "DONE" for part in message.get("content", [])) for message in assistants), assistants
                else:
                    assert str(captured) == session
                    offset = len(rpc.events)
                    rpc.request("B-notice", "prompt", message="PROCESS_B_NOTICE\nUse Python exactly once, then reply DONE.")
                    notice_events = wait_settled(rpc, offset, owned)
                    ends = [event for event in notice_events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
                    assert len(ends) == 1 and ends[0].get("isError") is True and "Python workspace started fresh; state from an earlier process is not restored" in json.dumps(ends[0]), ends
                    offset = len(rpc.events)
                    rpc.request("B-name", "prompt", message="PROCESS_B_NAME\nUse Python exactly once, then reply DONE.")
                    name_events = wait_settled(rpc, offset, owned)
                    ends = [event for event in name_events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
                    assert len(ends) == 1 and ends[0].get("isError") is True and "NameError" in json.dumps(ends[0]), ends
                generation = None
                deadline = time.monotonic() + 10
                sidecar = str(source.parent / "python/sidecar.py").encode()
                while time.monotonic() < deadline:
                    owned.remaining()
                    candidates = [(pid, start) for pid, start in owned.seen.items()
                                  if Path(f"/proc/{pid}/cmdline").exists() and sidecar in Path(f"/proc/{pid}/cmdline").read_bytes()]
                    if candidates:
                        generation = min(candidates)
                        break
                    time.sleep(.02)
                assert generation is not None
                current_pi = isolation.process_info(proc.pid)[0]
                if label == "B":
                    assert (proc.pid, current_pi) != prior_pi and generation != prior_kernel
                else:
                    prior_pi, prior_kernel = (proc.pid, current_pi), generation
                orderly_stop(proc, rpc, owned)
            finally:
                owned.cleanup()
                rpc.close()
                if not proc.stdin.closed:
                    proc.stdin.close()
                proc.stdout.close()
    print("process-exit fresh notice/NameError, distinct A-B identity, 0 survivors", flush=True)


def installed_checks(env, cwd, selected):
    with tempfile.TemporaryDirectory(prefix="pi-qwy-source-") as directory:
        installed = Path(selected) if selected else Path(directory) / "independent"
        if not selected:
            (installed / "packages/rlm/python").mkdir(parents=True)
            shutil.copy2(ROOT.parents[1] / "package.json", installed / "package.json")
            for name in RUNTIME_FILES:
                shutil.copy2(ROOT / name, installed / "packages/rlm" / name)
        installed = independent_source(installed)
        originals = {relative: ROOT.parents[1] / relative for relative in ("package.json", *("packages/rlm/" + name for name in RUNTIME_FILES))}
        copies = {relative: installed / relative for relative in originals}
        # Verify every source/copy pair, including lock/project, before uv cache preflight or runtime launch.
        assert all(sha256(originals[name]) == sha256(copies[name]) for name in originals)
        assert all(not originals[name].samefile(copies[name]) for name in originals)
        assert all(originals[name].resolve() != copies[name].resolve() for name in originals)
        settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings.write_text(json.dumps({"packages": [{"source": str(installed),
            "extensions": ["+packages/rlm/index.ts"], "skills": [], "prompts": [], "themes": []}],
            "extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"]}))
        automatic = run_probe(env, cwd, installed, "automatic")
        injected = run_probe(env, cwd, installed, "injected")
        assert automatic.isdisjoint(injected), "automatic and injected processes shared an identity"
        print("installed automatic/injected Pi and kernel PID+starttime identities distinct", flush=True)
        process_exit_check(env, cwd, installed)
        bootstrap_check(env, cwd, installed, "missing-executable")
        bootstrap_check(env, cwd, installed, "process-exit78")
    print("installed provenance and session-only semantics passed", flush=True)

def legacy_preflight(cache):
    """Reject unavailable locked Jupyter prerequisites before lifecycle launch."""
    with tempfile.TemporaryDirectory(prefix="pi-mbl3-preflight-control-") as directory:
        root = Path(directory)
        attempt = root / "attempt"
        fake = root / "uv"
        fake.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\nPath({str(attempt)!r}).write_text('1')\nsys.exit(7)\n")
        fake.chmod(0o700)
        env = {**os.environ, "UV_OFFLINE": "1", "UV_CACHE_DIR": str(root / "empty-cache"),
               "UV_PROJECT_ENVIRONMENT": str(root / "environment")}
        result = isolation.run_owned_capture([str(fake), "sync", "--project", str(ROOT / "python"), "--locked", "--no-dev"],
                                             cwd=ROOT, env=env, timeout=5)
        assert result.returncode == 7 and attempt.read_text() == "1"
        print("legacy-state preflight negative: one fake uv attempt; lifecycle starts 0; online fallback 0", flush=True)
    disposable = Path(tempfile.mkdtemp(prefix="pi-mbl3-preflight-"))
    try:
        env = {**os.environ, "UV_OFFLINE": "1", "UV_CACHE_DIR": str(cache),
               "UV_PROJECT_ENVIRONMENT": str(disposable / "environment")}
        result = isolation.run_owned_capture(["uv", "sync", "--project", str(ROOT / "python"), "--locked", "--no-dev"],
                                             cwd=ROOT, env=env, timeout=15)
        return result.returncode == 0 and (disposable / "environment/bin/python").is_file()
    finally:
        shutil.rmtree(disposable, ignore_errors=True)
        assert not disposable.exists()


def session_fixture(path, cwd):
    timestamp = "2026-10-29T00:00:00.000Z"
    header = {"type": "session", "version": 3, "id": "00000000-0000-4000-8000-00000000b103", "timestamp": timestamp, "cwd": str(cwd)}
    entry = {"type": "message", "id": "b1030001", "parentId": None, "timestamp": timestamp,
             "message": {"role": "user", "content": "validated legacy-state fixture", "timestamp": 1793232000000}}
    path.write_text(json.dumps(header) + "\n" + json.dumps(entry) + "\n")
    path.chmod(0o600)
    return [header, entry]


def validate_session(env, cwd, session, expected):
    command = [env["PI_BIN"], "--mode", "rpc", "--session", str(session), "--no-extensions"]
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            state = rpc.request("state", "get_state", timeout=15)
            entries = rpc.request("entries", "get_entries", timeout=15)
            assert state["success"] is True and Path(state["data"]["sessionFile"]).resolve() == session.resolve()
            actual = entries.get("data", {}).get("entries", [])
            fixture = expected[1]
            assert entries["success"] is True and any(entry.get("type") == "message" and entry.get("id") == fixture["id"]
                and entry.get("parentId") is None and entry.get("message", {}).get("role") == "user"
                and entry.get("message", {}).get("content") == fixture["message"]["content"] for entry in actual), actual
            orderly_stop(proc, rpc, owned)
        finally:
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
    print("legacy-state exact v3 session: get_state/get_entries validated; 0 survivors", flush=True)


def legacy_targets(root):
    names = ("kernel.bin", "kernel.names.json", "kernel.tmp", "kernel.names.tmp", "kernel.corrupt")
    root.mkdir(parents=True, mode=0o700)
    paths = [root / name for name in names]
    for index, path in enumerate(paths):
        path.write_bytes((f"legacy-{index}").encode())
        path.chmod(0o600)
    return paths


def snapshot_targets(paths):
    return {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino) for path in paths}


def legacy_phases(rpc, owned, log):
    offset = len(rpc.events)
    assert rpc.request("startup-notice", "prompt", message="LEGACY startup-notice\nUse Python exactly once, then reply DONE.")["data"]["disposition"] == "started"
    notice_events = wait_settled(rpc, offset, owned, timeout=15)
    notice_ends = [event for event in notice_events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
    assert len(notice_ends) == 1 and notice_ends[0].get("isError") is True and "Python workspace started fresh" in json.dumps(notice_ends[0])
    expected = [
        ("success", {"code": "legacy_value = 42\nlegacy_value"}, False),
        ("failed-mutation", {"code": "legacy_value = 43\nraise RuntimeError('legacy mutation failed')"}, True),
        ("variables", {"operation": "variables"}, False),
        ("reset", {"operation": "reset"}, False),
        ("post-reset", {"code": "legacy_value"}, True),
    ]
    for phase, arguments, is_error in expected:
        offset = len(rpc.events)
        assert rpc.request(phase, "prompt", message=f"LEGACY {phase}\nUse Python exactly once, then reply DONE.")["data"]["disposition"] == "started"
        events = wait_settled(rpc, offset, owned, timeout=15)
        ends = [event for event in events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
        assert len(ends) == 1 and ends[0].get("isError") is is_error, (phase, events)
    assert rpc.request("reload", "prompt", message="/legacy-reload")["data"]["disposition"] == "handled"
    offset = len(rpc.events)
    rpc.request("reload-notice", "prompt", message="LEGACY reload-notice\nUse Python exactly once, then reply DONE.")
    events = wait_settled(rpc, offset, owned, timeout=15)
    ends = [event for event in events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
    assert len(ends) == 1 and ends[0].get("isError") is True and "Python workspace started fresh" in json.dumps(ends[0]), ends
    offset = len(rpc.events)
    rpc.request("post-reload", "prompt", message="LEGACY post-reload\nUse Python exactly once, then reply DONE.")
    events = wait_settled(rpc, offset, owned, timeout=15)
    ends = [event for event in events if event.get("type") == "tool_execution_end" and event.get("toolName") == "python"]
    assert len(ends) == 1 and ends[0].get("isError") is True and "NameError" in json.dumps(ends[0]), ends
    records = [json.loads(line) for line in log.read_text().splitlines()]
    starts = [record for record in records if record["type"] == "start"]
    calls = [record for record in records if record["type"] == "call"]
    results = [record for record in records if record["type"] == "result"]
    assert len(starts) >= 2 and all(record["tools"] == 1 for record in starts)
    assert [record["arguments"] for record in calls] == [{"code": "1"}] + [item[1] for item in expected] + [{"code": "1"}, {"code": "legacy_value"}]
    assert [record["isError"] for record in results] == [True] + [item[2] for item in expected] + [True, True]


def run_legacy_lifecycle(env, cwd, installed, session, targets, *, mutant):
    log = cwd / ("mutant.jsonl" if mutant else "candidate.jsonl")
    case_env = {**env, "PI_RLM_PROBE_MODE": "legacy-state", "PI_RLM_LEGACY_LOG": str(log),
                "PI_RLM_ENV_DIR": str(cwd / ("mutant-venv" if mutant else "candidate-venv"))}
    command = [env["PI_BIN"], "--mode", "rpc", "--session", str(session), "--tools", "python",
               "--provider", "rlm-installed-local", "--model", "fixed", "--thinking", "off",
               "--extension", str(ROOT / "installed-probe.ts")]
    calibration = targets[0].parent / ("mutant-calibration" if mutant else "candidate-calibration")
    calibration.write_bytes(b"calibrate")
    before = snapshot_targets(targets)
    siblings = {path.name for path in targets[0].parent.iterdir()}
    with legacy_sensor.LegacyStateSensor(targets, calibration) as sensor, tempfile.TemporaryFile() as stderr:
        sensor.calibrate()
        baseline = sensor.drain()
        proc = subprocess.Popen(command, cwd=cwd, env=case_env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=stderr, start_new_session=True)
        assert proc.stdin is not None and proc.stdout is not None
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["data"]["sessionFile"] == str(session)
            legacy_phases(rpc, owned, log)
            orderly_stop(proc, rpc, owned)
            assert not owned.remaining()
            counts = sensor.final_drain()
        except BaseException:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
    after = snapshot_targets(targets)
    assert after == before and {path.name for path in targets[0].parent.iterdir()} == siblings
    deltas = {path.name: {category: counts[path][category] - baseline[path][category]
                         for category in ("access", "write", "delete")} for path in targets}
    assert sum(len(value) for value in deltas.values()) == 15
    assert all(value["write"] == value["delete"] == 0 for value in deltas.values())
    if mutant:
        assert deltas[targets[0].name]["access"] > 0
        assert all(value["access"] == 0 for name, value in deltas.items() if name != targets[0].name)
        print("legacy-state mutant expected-fail: kernel.bin access>0 only; writes/deletes=0; 15 categories; integrity unchanged", flush=True)
    else:
        assert all(value == {"access": 0, "write": 0, "delete": 0} for value in deltas.values())
        print("legacy-state candidate: exact 15 categories zero; integrity unchanged; 0 survivors", flush=True)


def legacy_state_checks(env, cwd):
    cache = Path(env["UV_CACHE_DIR"])
    if not legacy_preflight(cache):
        print("BLOCKED: approved offline Jupyter prerequisites unavailable; preparation/download approval required", file=sys.stderr, flush=True)
        return 69
    with tempfile.TemporaryDirectory(prefix="pi-mbl3-source-") as directory:
        fixture = Path(directory)
        candidate, mutant = fixture / "candidate", fixture / "mutant"
        for installed in (candidate, mutant):
            (installed / "packages/rlm/python").mkdir(parents=True)
            shutil.copy2(ROOT.parents[1] / "package.json", installed / "package.json")
            for name in RUNTIME_FILES:
                shutil.copy2(ROOT / name, installed / "packages/rlm" / name)
            independent_source(installed)
            for relative in ("python/pyproject.toml", "python/uv.lock"):
                assert sha256(installed / "packages/rlm" / relative) == sha256(ROOT / relative)
        index = mutant / "packages/rlm/index.ts"
        source = index.read_text()
        needle = '  pi.on("session_start", async (event: any, ctx) => {\n'
        delta = '  pi.on("session_start", async (event: any, ctx) => {\n    const legacy = join(process.env["PI_RLM_STATE_DIR"]!, createHash("sha256").update(ctx.sessionManager.getSessionFile()!).digest("hex"), "kernel.bin");\n    readFileSync(legacy);\n'
        assert needle in source and "createHash" not in source and "readFileSync" not in source
        source = source.replace('import { homedir } from "node:os";\n', 'import { createHash } from "node:crypto";\nimport { readFileSync } from "node:fs";\nimport { homedir } from "node:os";\n').replace(needle, delta)
        index.write_text(source)
        settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings.write_text(json.dumps({"packages": [{"source": str(candidate), "extensions": ["+packages/rlm/index.ts"], "skills": [], "prompts": [], "themes": []}],
            "extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"]}))
        session = cwd / "legacy-v3.jsonl"
        expected = session_fixture(session, cwd)
        validate_session(env, cwd, session, expected)
        historical = Path(env["PI_RLM_STATE_DIR"]) / hashlib.sha256(str(session.resolve()).encode()).hexdigest()
        targets = legacy_targets(historical)
        run_legacy_lifecycle(env, cwd, candidate, session, targets, mutant=False)
        settings.write_text(settings.read_text().replace(str(candidate), str(mutant)))
        run_legacy_lifecycle(env, cwd, mutant, session, targets, mutant=True)
    print("legacy-state PASS: registered package lifecycle zero access; read-only installed index mutant rejected", flush=True)
    return 0


def worker(case="all", installed_root=None):
    # A fresh worker has no unrelated children. Reuse the proven positive-owned
    # tree/subreaping helper, with an outer owned supervisor as failure backup.
    with isolation.subreaper():
        env = {**os.environ, "UV_OFFLINE": "1"}
        cwd = Path.cwd()
        if case == "legacy-state":
            raise SystemExit(legacy_state_checks(env, cwd))
        if case == "installed":
            installed_checks(env, cwd, installed_root)
            return
        if case == "operations":
            output = cwd / "probe.json"
            env_dir = Path(tempfile.mkdtemp(prefix="pi-rlm-operations-env-"))
            try:
                output.unlink(missing_ok=True)
                pi_check({**env, "PI_RLM_ENV_DIR": str(env_dir)}, cwd, ROOT / "index-probe.ts", output, "EOF", injected=True)
                output.unlink(missing_ok=True)
            finally:
                shutil.rmtree(env_dir, ignore_errors=True)
            print("mbl operations PASS", flush=True)
            return
        protocol_checks(env, cwd)
        env_dir = cwd / "rlm-integration-env"
        env_dir.mkdir(mode=0o700)
        env = {**env, "PI_RLM_ENV_DIR": str(env_dir)}
        output = cwd / "probe.json"
        pi_check(env, cwd, ROOT / "index-probe.ts", output, "EOF", injected=True)
        probe = cwd / "registration.ts"
        probe.write_text("import {writeFileSync} from 'node:fs';\nexport default function(pi) {\n" +
                         "pi.registerCommand('probe-rlm', {description:'Registration only', handler:async () => {\n" +
                         "writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify(pi.getAllTools().find(t => t.name === 'python')));\n}});}\n")
        for mode in ("EOF", "TERM", "KILL"):
            pi_check(env, cwd, probe, output, mode)
        installed_checks(env, cwd, installed_root)
        shutil.rmtree(env_dir)
    print("rlm extension integration check passed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "installed", "operations", "legacy-state"), default="all")
    parser.add_argument("--installed-root", type=Path, help="independent package root (otherwise an owned copy fixture)")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        worker(args.case, args.installed_root)
    else:
        selected = independent_source(args.installed_root) if args.installed_root else None
        with isolation.isolated_environment(discover_extensions=True) as env:
            command = [sys.executable, str(Path(__file__).resolve()), "--_worker", "--case", args.case]
            if selected:
                command.extend(["--installed-root", str(selected)])
            inner_timeout = {"operations": 55, "installed": 75, "all": 110, "legacy-state": 220}[args.case]
            result = isolation.run_owned_capture(command, cwd=Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd",
                                                 env=env, timeout=inner_timeout)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        sys.exit(result.returncode)
