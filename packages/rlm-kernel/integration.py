#!/usr/bin/env python3
"""Offline actual-index wiring and real-pi cleanup checks (Linux/pi 1.1.0)."""
import importlib.util
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("isolation", ROOT / "isolated-check.py")
isolation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(isolation)


class Rpc:
    """Drain binary stdout on LF with real wall deadlines, never blocking readline."""
    def __init__(self, proc):
        self.proc = proc
        self.selector = selectors.DefaultSelector()
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.responses = {}
        self.eof = False

    def pump(self, deadline):
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
            if len(self.buffer) > 1024 * 1024 or len(self.responses) > 256:
                raise RuntimeError("RPC protocol buffer limit exceeded")

    def request(self, request_id, request_type, timeout=10, **values):
        self.proc.stdin.write(json.dumps({"id": request_id, "type": request_type, **values}).encode() + b"\n")
        self.proc.stdin.flush()
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
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc)
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
    owned = isolation.OwnedProcesses(proc)
    rpc = Rpc(proc)
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
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", message="/probe-rlm")["success"] is True
            wait_until(output.exists, rpc, owned, timeout=20)
            result = json.loads(output.read_text())
            assert "error" not in result, result
            if injected:
                assert result["passed"] >= 3, result
            else:
                assert result["name"] == "python" and result["exposure"] == "model-only", result
                assert "pi.read" in result["description"], result
                wait_until(lambda: len(owned.remaining()) >= 4, rpc, owned)
                identities = dict(owned.seen)
                assert len(identities) >= 4, "real pi/uv/Python/Monty must be observed"
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
                assert all(isolation.process_info(pid) != (start, None) and
                           (info := isolation.process_info(pid)) is None for pid, start in identities.items()), identities
            print(f"real pi {mode}: {result}, 0 survivors", flush=True)
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


def worker():
    # A fresh worker has no unrelated children. Reuse the proven positive-owned
    # tree/subreaping helper, with an outer owned supervisor as failure backup.
    with isolation.subreaper():
        env = dict(os.environ)
        cwd = Path.cwd()
        protocol_checks(env, cwd)
        output = cwd / "probe.json"
        pi_check(env, cwd, ROOT / "index-probe.ts", output, "EOF", injected=True)
        probe = cwd / "registration.ts"
        probe.write_text("import {writeFileSync} from 'node:fs';\nexport default function(pi) {\n" +
                         "pi.registerCommand('probe-rlm', {description:'Registration only', handler:async () => {\n" +
                         "writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify(pi.getAllTools().find(t => t.name === 'python')));\n}});}\n")
        for mode in ("EOF", "TERM", "KILL"):
            pi_check(env, cwd, probe, output, mode)
    print("rlm-kernel extension integration check passed", flush=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--_worker"]:
        worker()
    else:
        with isolation.isolated_environment() as env:
            result = isolation.run_owned_capture([sys.executable, str(Path(__file__).resolve()), "--_worker"],
                                                 cwd=Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd",
                                                 env=env, timeout=35)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        sys.exit(result.returncode)
