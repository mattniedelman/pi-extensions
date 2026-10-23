#!/usr/bin/env python3
"""Bounded no-inference actual-pi renderer probe; all fixtures are private."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("integration", ROOT / "integration.py")
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)
runner = integration.isolation


def worker():
    env = dict(os.environ)
    cwd = Path.cwd()
    output = cwd / "render-result.json"
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--extension", str(ROOT / "render-probe.ts")]
    with runner.subreaper(), tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "PI_RLM_PROBE_OUTPUT": str(output)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                start_new_session=True)
        owned = runner.OwnedProcesses(proc)
        rpc = integration.Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"]
            assert rpc.request("render", "prompt", message="/probe-rlm")["success"]
            integration.wait_until(output.exists, rpc, owned, timeout=5)
            report = json.loads(output.read_text())
            assert "error" not in report, report
            assert report["passed"] == 10, report
            proc.stdin.close()
            assert proc.wait(timeout=5) == 0
            assert not owned.remaining(), "renderer fixture survivors BEFORE backup"
            print(json.dumps(report), flush=True)
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


if __name__ == "__main__":
    if sys.argv[1:] == ["--_worker"]:
        worker()
    else:
        with runner.isolated_environment() as env:
            private = Path(env["PI_CODING_AGENT_DIR"]).parent
            result = runner.run_owned_capture([sys.executable, str(Path(__file__).resolve()), "--_worker"],
                                              cwd=private / "cwd", env=env, timeout=12)
        assert not private.exists()
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        sys.exit(result.returncode)
