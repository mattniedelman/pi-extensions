#!/usr/bin/env python3
"""No-inference real-pi permission replay; standalone Linux/pi 1.1.0 proof."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import permissions as p

ROOT = Path(__file__).resolve().parent
SELECTOR = "q1s-local-replay/fixed-python"
CASES = ("good", "readBeforeDeny", "lostHook")


def replay_case(env, cwd, installed, case):
    (cwd / "policy.jsonl").unlink(missing_ok=True)
    output = io.StringIO()
    assertion = None
    with contextlib.redirect_stdout(output):
        try:
            p.live_check({**env, "PI_RLM_REPLAY_CASE": case}, cwd, installed, SELECTOR, replay=True)
        except AssertionError as error:
            assertion = str(error)
    text = output.getvalue()
    assert "IUA_PRIVATE_" not in text, "unsafe replay report"
    lines = [line for line in text.splitlines() if line.startswith("permissions live ")]
    assert len(lines) == 1, "complete replay diagnostics missing"
    report = json.loads(lines[0].split(": ", 1)[1])
    print("permissions replay " + case + " " + ("PASS: " if report["passed"] else "FAIL: ") +
          json.dumps(report, sort_keys=True), flush=True)
    failed = {name for name, value in report["checks"].items() if value == "FAIL"}
    assert set(report["sensorPhases"]) == set(p.PHASES), "replay sensor phases missing"
    assert len(report["checks"]) == 36, "replay check count changed"
    assert report["survivorsBeforeBackup"] == 0, "owned survivors BEFORE backup"
    assert not p.isolation.children(os.getpid()), "replay children survived finally cleanup"
    print("replay finally " + case + ": 0 owned children; 0 survivors BEFORE backup", flush=True)
    if case == "good":
        assert report["passed"] and assertion is None and not failed, "real callback replay rejected"
    else:
        assert not report["passed"] and assertion == "permissions invariant failure", "negative strict verdict lost"
        assert report["sensorPhases"]["afterSettled"]["denied.txt"] > 0, "negative actual denied IO missing"
        if case == "readBeforeDeny":
            assert failed == {"sensor"}, "read-before-denial negative did not isolate sensor"
        else:
            assert {"sensor", "rpc_privacy", "hook_results", "python_result_envelope"} <= failed, "lost hook escaped oracle"
        print("replay negative " + case + ": expected strict FAIL discriminated actual denied IO", flush=True)


def worker():
    if p.isolation.children(os.getpid()):
        raise RuntimeError("replay worker must start child-free")
    env = {**os.environ, "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cwd = Path.cwd()
    command = lambda *argv: p.installation.command(env, cwd, *argv)
    with p.isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-q1s-source-") as directory:
        fixture = Path(directory)
        original, installed = fixture / "original", fixture / "installed"
        sha = command("git", "-C", p.installation.REPO, "rev-parse", "HEAD")
        command("git", "clone", "--no-hardlinks", "--no-checkout", p.installation.REPO, original)
        command("git", "-C", original, "checkout", "--detach", sha)
        command("git", "clone", "--no-hardlinks", original, installed)
        command("git", "-C", installed, "checkout", "--detach", sha)
        command("git", "-C", installed, "remote", "remove", "origin")
        p.installation.committed_source(env, cwd, installed, sha)
        original.rename(fixture / "unavailable-original")
        assert not original.exists(), "original fixture remained available"
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
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm-kernel/index.ts"],
                                   "skills": [], "prompts": [], "themes": []}]
        settings.write_text(json.dumps(configured))
        settings.chmod(0o600)
        print("replay committed source: " + sha + "; independent clone; original fixture unavailable", flush=True)
        for case in CASES:
            replay_case(env, cwd, installed, case)
    assert not fixture.exists(), "owned source fixture survived"
    print("replay owned committed source fixtures: absent", flush=True)


def run():
    # Outside all dedicated ownership supervisors: it must never be signaled.
    with tempfile.TemporaryDirectory(prefix="pi-q1s-unrelated-") as directory:
        root = Path(directory)
        sentinel = subprocess.Popen([sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"],
            env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert sentinel.stdout.readline() == "ready\n", "unrelated sentinel not ready"
            with p.isolation.isolated_environment(discover_extensions=True) as env:
                agent = Path(env["PI_CODING_AGENT_DIR"])
                result = p.isolation.run_owned_capture([sys.executable, str(ROOT / "permissions_replay.py"), "--_worker"],
                    cwd=agent.parent / "cwd", env=env, timeout=36)
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            assert not agent.parent.exists(), "private agent/cwd/state/session root survived"
            assert sentinel.poll() is None, "unrelated sentinel signaled"
            print("replay private agent/cwd/state/session/source: absent; unrelated sentinel alive", flush=True)
            return result.returncode
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=2)
            sentinel.stdout.close()
    

if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--_worker"]:
            worker()
        else:
            assert not sys.argv[1:], "unsupported replay argv"
            sys.exit(run())
    except Exception as error:
        print("permissions replay failure: " + p.safe_error_type(error), file=sys.stderr, flush=True)
        sys.exit(1)
