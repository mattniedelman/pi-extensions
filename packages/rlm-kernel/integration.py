#!/usr/bin/env python3
"""Offline actual-index wiring and real-pi cleanup checks (Linux/pi 1.1.0)."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shutil
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
    def __init__(self, proc, owned):
        self.proc = proc
        self.owned = owned
        self.selector = selectors.DefaultSelector()
        self.selector.register(proc.stdout, selectors.EVENT_READ)
        self.buffer = b""
        self.responses = {}
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
            if len(self.buffer) > 1024 * 1024 or len(self.responses) > 256:
                raise RuntimeError("RPC protocol buffer limit exceeded")

    def request(self, request_id, request_type, timeout=10, **values):
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
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", message="/probe-rlm")["success"] is True
            wait_until(output.exists, rpc, owned, timeout=20)
            result = json.loads(output.read_text())
            assert "error" not in result, result
            if injected:
                assert result["passed"] == 24 and result["wiringOnly"] is True, result
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


RUNTIME_FILES = ("index.ts", "client.mjs", "bridge.mjs", "python/sidecar.py",
                 "python/pyproject.toml", "python/uv.lock")


def independent_source(root):
    root = root.resolve(strict=True)
    original = ROOT.parents[1]
    assert not root.is_relative_to(original), "installed root must be independent of original worktree"
    for relative in ("package.json", *("packages/rlm-kernel/" + name for name in RUNTIME_FILES)):
        path = root / relative
        assert path.resolve(strict=True).is_relative_to(root), f"installed source escapes independent root: {relative}"
        assert not path.samefile(original / relative), f"installed source aliases original: {relative}"
    return root


def installed_check(env, cwd, installed, mode="ready"):
    """Reusable oracle for zog after installation; no install/list/remove here."""
    installed = independent_source(installed)
    source = installed / "packages/rlm-kernel/index.ts"
    fixture = cwd / ("installed-" + mode)
    fixture.mkdir()
    for name in ("state", "sessions", "bin"):
        (fixture / name).mkdir(mode=0o700)
    case_env = {**env, "PI_RLM_STATE_DIR": str(fixture / "state"),
                "PI_CODING_AGENT_SESSION_DIR": str(fixture / "sessions")}
    if mode != "ready":
        # Only owned fixtures on PATH: uv genuinely cannot be found in missing-uv.
        node = shutil.which("node", path=env["PATH"])
        if node:
            (fixture / "bin/node").symlink_to(node)
        case_env["PATH"] = str(fixture / "bin")
        if mode == "failed-bootstrap":
            uv = fixture / "bin/uv"
            uv.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write('qwy bootstrap deliberately failed\\n')\nsys.exit(78)\n")
            uv.chmod(0o700)
    loader = fixture / "loader.ts"
    loader.write_text(f"import factory from {json.dumps(str(source))};\n" +
                      f"import probe from {json.dumps(str(ROOT / 'installed-probe.ts'))};\n" +
                      f"export default host => probe(host, factory, {json.dumps(str(source))});\n")
    output = fixture / "result.json"
    command = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--extension", str(loader)]
    started = time.monotonic()
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(command, cwd=cwd, env={**case_env, "PI_RLM_PROBE_OUTPUT": str(output)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        owned = isolation.OwnedProcesses(proc)
        rpc = Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", message="/probe-rlm")["success"] is True
            wait_until(output.exists, rpc, owned, timeout=12)
            result = json.loads(output.read_text())
            # Discovery and actual runtime replies are conjunctive, never metadata-only success.
            assert result.get("discovered") == {"name": "python", "exposure": "model-only"}, result
            assert result["source"] == str(source.resolve()), result
            assert result["sourceIdentity"] == {"device": source.stat().st_dev, "inode": source.stat().st_ino}, result
            assert result["closed"] is True, result
            if mode == "ready":
                assert result.get("ready") is True and "error" not in result, result
                assert result["values"] == [42, 43] and len(result["generations"]) == 2, result
                # Both auto-loaded discovery and injected runtime must execute the installed sidecar.
                for generation in (result["automatic"], *result["generations"]):
                    assert any(str(source.parent / "python/sidecar.py") in child["argv"] for child in generation), generation
                    assert all(str(ROOT / "python/sidecar.py") not in child["argv"] for child in generation), generation
                    parents = {child["pid"] for child in generation}
                    assert all(child["parent"] == proc.pid or child["parent"] in parents for child in generation), generation
            else:
                assert result.get("ready") is False and result.get("phase") == "checkpoint", result
                error = result.get("error", "")
                if mode == "missing-uv":
                    assert ("ENOENT" in error or "Executable not found" in error) and "uv" in error, result
                else:
                    assert "78" in error and "qwy bootstrap deliberately failed" in error, result
                assert time.monotonic() - started < 8, "bootstrap failure was not bounded"
            # Evidence is emitted before cleanup, including explicit rejection of negatives.
            print(f"installed oracle {mode}: {json.dumps(result, sort_keys=True)}", flush=True)
            proc.stdin.close()
            proc.wait(timeout=5)
            assert proc.returncode == 0, proc.returncode
            deadline = time.monotonic() + 3
            while owned.remaining() and time.monotonic() < deadline:
                time.sleep(.01)
            assert not owned.remaining(), "installed pi left owned descendants BEFORE backup"
        except BaseException:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            owned.cleanup()
            assert not owned.remaining()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
    print(f"installed {mode}: 0 survivors BEFORE outer backup", flush=True)


def installed_checks(env, cwd, selected):
    with tempfile.TemporaryDirectory(prefix="pi-qwy-source-") as directory:
        installed = Path(selected) if selected else Path(directory) / "independent"
        if not selected:
            (installed / "packages/rlm-kernel/python").mkdir(parents=True)
            shutil.copy2(ROOT.parents[1] / "package.json", installed / "package.json")
            for name in RUNTIME_FILES:
                shutil.copy2(ROOT / name, installed / "packages/rlm-kernel" / name)
        installed = independent_source(installed)
        # A private configured package, RLM-only filter; not an original index --extension.
        settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings.write_text(json.dumps({"packages": [{"source": str(installed),
            "extensions": ["+packages/rlm-kernel/index.ts"], "skills": [], "prompts": [], "themes": []}],
            "extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"]}))
        for mode in ("ready", "missing-uv", "failed-bootstrap"):
            installed_check(env, cwd, installed, mode)
    print("installed readiness: 1 positive (42/43, checkpoint/variables, restore), 2 bootstrap negatives passed", flush=True)


def worker(case="all", installed_root=None):
    # A fresh worker has no unrelated children. Reuse the proven positive-owned
    # tree/subreaping helper, with an outer owned supervisor as failure backup.
    with isolation.subreaper():
        env = {**os.environ, "UV_OFFLINE": "1"}
        cwd = Path.cwd()
        if case == "installed":
            installed_checks(env, cwd, installed_root)
            return
        protocol_checks(env, cwd)
        output = cwd / "probe.json"
        pi_check(env, cwd, ROOT / "index-probe.ts", output, "EOF", injected=True)
        probe = cwd / "registration.ts"
        probe.write_text("import {writeFileSync} from 'node:fs';\nexport default function(pi) {\n" +
                         "pi.registerCommand('probe-rlm', {description:'Registration only', handler:async () => {\n" +
                         "writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify(pi.getAllTools().find(t => t.name === 'python')));\n}});}\n")
        for mode in ("EOF", "TERM", "KILL"):
            pi_check(env, cwd, probe, output, mode)
        installed_checks(env, cwd, installed_root)
    print("rlm-kernel extension integration check passed", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "installed"), default="all")
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
            result = isolation.run_owned_capture(command, cwd=Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd",
                                                 env=env, timeout=35)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        sys.exit(result.returncode)
