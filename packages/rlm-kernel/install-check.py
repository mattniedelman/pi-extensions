#!/usr/bin/env python3
"""Bounded offline install/discovery/remove of an independent committed package."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
spec = importlib.util.spec_from_file_location("integration", ROOT / "integration.py")
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)
isolation = integration.isolation


def command(env, cwd, *argv):
    result = isolation.run_owned_capture(argv, cwd=cwd, env=env, timeout=8)
    assert result.returncode == 0, (argv, result.returncode, result.stdout, result.stderr)
    print(f"command {json.dumps(list(map(str, argv)))}: exit 0", flush=True)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    return result.stdout.strip()


def committed_source(env, cwd, source, expected):
    """Reject dirty/uncommitted runtime or a clone sharing disposable objects."""
    assert command(env, cwd, "git", "-C", source, "rev-parse", "HEAD") == expected
    assert not (source / ".git/objects/info/alternates").exists(), "alternate object dependency"
    assert not command(env, cwd, "git", "-C", source, "status", "--porcelain"), "dirty installed checkout"
    for relative in ("package.json", *("packages/rlm-kernel/" + name for name in integration.RUNTIME_FILES)):
        # Compare only source, never user checkpoints. Dirty originals cannot be
        # quietly substituted for their committed counterpart in the fixture.
        committed = command(env, cwd, "git", "-C", source, "rev-parse", f"HEAD:{relative}")
        actual = command(env, cwd, "git", "hash-object", str(REPO / relative))
        assert actual == committed, f"uncommitted runtime: {relative}"
    return integration.independent_source(source)


def removed_discovery(env, cwd):
    """Actual fresh host loadout must lack python and spawn no child at all."""
    probe = cwd / "removed-probe.ts"
    output = cwd / "removed.json"
    probe.write_text("import {writeFileSync} from 'node:fs';\n" +
                     "export default host => host.registerCommand('probe-rlm', {\n" +
                     "description:'Assert removed loadout', handler:async () => {\n" +
                     "writeFileSync(process.env.PI_RLM_PROBE_OUTPUT, JSON.stringify({\n" +
                     "python:host.getAllTools().filter(t => t.name === 'python')}));\n}});\n")
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--extension", str(probe)]
    # Popen/RPC stays inside the dedicated subreaping worker. Its outer
    # supervisor is a failure backup, not the normal absence oracle.
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "PI_RLM_PROBE_OUTPUT": str(output)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                start_new_session=True)
        owned = isolation.OwnedProcesses(proc)
        rpc = integration.Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", message="/probe-rlm")["success"] is True
            integration.wait_until(output.exists, rpc, owned)
            assert json.loads(output.read_text()) == {"python": []}, "removed python still discovered"
            # Several event-loop turns plus ongoing ownership sampling, not
            # merely a registration-time snapshot before session_start.
            deadline = time.monotonic() + .2
            while time.monotonic() < deadline:
                assert set(owned.remaining()) <= {proc.pid}, "removed host spawned a child"
                time.sleep(.01)
            assert all(pid == proc.pid for pid, _ in owned.observed), "removed host spawned an observed child"
            proc.stdin.close()
            proc.wait(timeout=5)
            assert proc.returncode == 0
            assert not owned.remaining(), "removed host left children BEFORE backup"
            print("removed discovery: python absent, 0 observed children, 0 survivors BEFORE backup", flush=True)
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


def private_snapshot(paths):
    # Only owned fixture bytes are compared; do not emit sensitive contents.
    return {str(path): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in paths}


def worker():
    env = {**os.environ, "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cwd = Path.cwd()
    settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
    settings.write_text(json.dumps({"extensions": ["-builtin:mcp", "-builtin:llama.cpp",
                                                   "-builtin:codemode", "-builtin:tool-search"]}))
    with isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-zog-source-") as directory:
        fixture = Path(directory)
        original = fixture / "original"
        installed = fixture / "installed"
        sha = command(env, cwd, "git", "-C", REPO, "rev-parse", "HEAD")
        command(env, cwd, "git", "clone", "--no-hardlinks", "--no-checkout", str(REPO), str(original))
        command(env, cwd, "git", "-C", original, "checkout", "--detach", sha)
        command(env, cwd, "git", "clone", "--no-hardlinks", str(original), str(installed))
        command(env, cwd, "git", "-C", installed, "checkout", "--detach", sha)
        command(env, cwd, "git", "-C", installed, "remote", "remove", "origin")
        installed = committed_source(env, cwd, installed, sha)
        # Only our fixture source is renamed, never the real user worktree.
        original.rename(fixture / "unavailable-original")
        assert not original.exists()
        print(f"independent committed source: {sha}; original fixture unavailable", flush=True)

        # Actual installer must create this declaration before filtering it.
        packages = json.loads(settings.read_text()).get("packages", [])
        assert packages == [str(installed)], f"actual installer declaration missing: {packages}"
        listing = command(env, cwd, env["PI_BIN"], "list")
        assert str(installed) in listing and str(original) not in listing, listing
        configured = json.loads(settings.read_text())
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm-kernel/index.ts"],
                                   "skills": [], "prompts": [], "themes": []}]
        settings.write_text(json.dumps(configured))
        before = settings.read_bytes()
        for mode in ("ready", "missing-uv", "failed-bootstrap"):
            integration.installed_check(env, cwd, installed, mode)
        assert settings.read_bytes() == before, "readiness rewrote installation"
        result = json.loads((cwd / "installed-ready/result.json").read_text())
        state = cwd / "installed-ready/state"
        key = hashlib.sha256(result["identities"]["injected"].encode()).hexdigest()
        session = state / key
        dump, names = session / "kernel.bin", session / "kernel.names.json"
        assert dump.is_file() and names.is_file(), "actual checkpoint and names required"
        assert stat.S_IMODE(session.stat().st_mode) == 0o700
        assert stat.S_IMODE(dump.stat().st_mode) == stat.S_IMODE(names.stat().st_mode) == 0o600
        assert (state / ".venv/pyvenv.cfg").is_file(), "actual private venv required"
        quarantine = session / "kernel.corrupt"
        marker = state / ".venv/zog-retention-marker"
        for path in (quarantine, marker):
            path.write_bytes(b"owned retained fixture\n")
            path.chmod(0o600)
        retained = [dump, names, quarantine, marker]
        snapshot = private_snapshot(retained)
        command(env, cwd, env["PI_BIN"], "remove", str(installed))
        assert not json.loads(settings.read_text()).get("packages"), "remove retained package declaration"
        listing = command(env, cwd, env["PI_BIN"], "list")
        assert str(installed) not in listing, listing
        removed_discovery(env, cwd)
        assert private_snapshot(retained) == snapshot, "uninstall changed retained private data"
        committed_source(env, cwd, installed, sha)
        print("removal retention: 4/4 private checkpoint/names/quarantine/venv markers unchanged", flush=True)
        # Explicit destructive cleanup only after retention and no-child proof.
        for mode in ("ready", "missing-uv", "failed-bootstrap"):
            shutil.rmtree(cwd / ("installed-" + mode))
        assert not any(path.exists() for path in retained)
        print("intentional owned cleanup: 4/4 retained files absent; 3/3 case roots absent", flush=True)
    assert not fixture.exists(), "owned clone fixture survived cleanup"
    print("install boundary: committed clone + install/list/discovery + readiness3 + remove/list/absence + retained cleanup PASS", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._worker:
        worker()
    else:
        with isolation.isolated_environment(discover_extensions=True) as env:
            result = isolation.run_owned_capture([sys.executable, str(Path(__file__).resolve()), "--_worker"],
                cwd=Path(env["PI_CODING_AGENT_DIR"]).parent / "cwd", env=env, timeout=50)
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        sys.exit(result.returncode)
