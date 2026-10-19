"""Tiny executable fixtures for isolated-check.py; no model or Monty probes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


# A separately grouped, TERM-resistant grandchild outlives its parent unless
# the runner adopts, kills and reaps it. PID records make cleanup non-vacuous.
TREE = """
import os, signal, subprocess, sys, time
from pathlib import Path
record = Path(sys.argv[1])
child = subprocess.Popen([sys.executable, '-c',
    'import os,signal,time; from pathlib import Path; '
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'Path(' + repr(str(record) + '.child') + ').write_text(str(os.getpid())); '
    'time.sleep(60)'], start_new_session=True)
while not Path(str(record) + '.child').exists(): time.sleep(.005)
record.write_text(str(os.getpid()))
Path(str(record) + '.env').write_text(os.environ.get('PI_CODING_AGENT_DIR', ''))
mode = sys.argv[2]
if mode == 'failure': assert False, 'deliberate fixture failure'
if mode == 'eof': sys.stdin.read(); sys.exit(0)
if mode == 'success': sys.exit(0)
if mode == 'exit7': sys.exit(7)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""


def assert_gone(record):
    pids = [int(record.read_text()), int(Path(str(record) + '.child').read_text())]
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids), pids


def self_test(runner):
    count = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pi-isolated-self-test-") as directory:
        root = Path(directory)
        fake_pi = root / "fake-pi"
        fake_pi.write_text(f"#!{sys.executable}\n" + "import json,os,sys\nfrom pathlib import Path\n" +
            "Path(sys.argv[-1]).write_text(json.dumps({'argv':sys.argv[1:-1], 'cwd':os.getcwd(), 'env':dict(os.environ)}))\n")
        fake_pi.chmod(0o700)
        poison = root / "personal"
        poison.mkdir()
        (poison / "settings.json").write_text('{"packages":["must-not-load"]}')
        original = (poison / "settings.json").read_bytes()
        source = {**os.environ, "PI_BIN": str(fake_pi), "PI_CODING_AGENT_DIR": str(poison),
                  "PI_SESSION_FILE": "personal-checkpoint", "OPENAI_API_KEY": "must-not-leak",
                  "AWS_PROFILE": "must-not-leak", "NODE_OPTIONS": "must-not-load"}
        with runner.isolated_environment(source) as env:
            private = Path(env["PI_CODING_AGENT_DIR"]).parent
            keys = ["PI_CODING_AGENT_DIR", "PI_RLM_STATE_DIR", "PI_CODING_AGENT_SESSION_DIR"]
            assert len({env[k] for k in keys}) == 3
            for key in keys:
                path = Path(env[key])
                assert path.is_relative_to(private) and path.stat().st_mode & 0o777 == 0o700
                assert not list(path.iterdir())
            assert env["HINDSIGHT_DISABLED"] == "true" and env["PI_OFFLINE"] == "1"
            assert all(key not in env for key in ["PI_SESSION_FILE", "OPENAI_API_KEY", "AWS_PROFILE", "NODE_OPTIONS"])
            assert env["UV_CACHE_DIR"] == source.get("UV_CACHE_DIR", str(Path(source.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "uv"))
            assert Path(env["UV_PROJECT_ENVIRONMENT"]).is_relative_to(private)
            report = root / "cwd.json"
            code = "import os; from pathlib import Path; Path(%r).write_text(os.getcwd())" % str(report)
            assert runner.run_owned([sys.executable, "-c", code], cwd=root, env=env, timeout=2) == 0
            assert report.read_text() == str(root), "caller cwd must be preserved for npm"
            count += 1
            pi_report = root / "pi.json"
            assert runner.run_owned(["pi", "--extension", "/explicit/index.ts", str(pi_report)], cwd=root, env=env, timeout=2) == 0
            result = json.loads(pi_report.read_text())
            assert Path(result["cwd"]).is_relative_to(private) and result["cwd"] != str(root)
            for flag in ["--no-extensions", "--no-context-files", "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes", "--offline"]:
                assert flag in result["argv"]
            assert result["argv"][-2:] == ["--extension", "/explicit/index.ts"]
            assert result["env"]["PI_CODING_AGENT_DIR"] == env["PI_CODING_AGENT_DIR"]
            count += 1
            # This sentinel is owned by the test, not the command under test.
            sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
            try:
                for mode, expected in [("success", 0), ("exit7", 7), ("failure", 1), ("eof", 0), ("hang", 124)]:
                    record = root / mode
                    before = time.monotonic()
                    status = runner.run_owned([sys.executable, "-c", TREE, str(record), mode], cwd=root, env=env, timeout=.5 if mode == "hang" else 2)
                    assert status == expected, (mode, status)
                    assert time.monotonic() - before < 3, mode
                    assert_gone(record)
                    assert sentinel.poll() is None, "cleanup touched an unrelated process group"
                    count += 1
                assert runner.run_owned([str(root / "missing-command")], cwd=root, env=env, timeout=1) == 127
                count += 1
            finally:
                sentinel.terminate()
                sentinel.wait(timeout=2)
        assert not private.exists(), "private directories survived cleanup"
        assert (poison / "settings.json").read_bytes() == original
        count += 1
        # Check *before* the surrounding runner's backup cleanup, so a broken
        # nested runner cannot pass merely because the outer runner rescued it.
        fixture = root / "tree.py"
        fixture.write_text(TREE)
        guard = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
record = Path(sys.argv[1])
command = sys.argv[2:]
proc = subprocess.Popen(command)
if os.environ['EXPECTED_STATUS'] == '130':
    deadline = time.monotonic() + 3
    while not Path(str(record) + '.env').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert Path(str(record) + '.env').exists(), 'fixture never became ready'
    os.kill(proc.pid, signal.SIGINT)
status = proc.wait(timeout=5)
assert status == int(os.environ['EXPECTED_STATUS']), status
pids = [int(record.read_text()), int(Path(str(record) + '.child').read_text())]
assert all(not Path('/proc/' + str(pid)).exists() for pid in pids), pids
assert not Path(Path(str(record) + '.env').read_text()).parent.exists(), 'runner private directories survived'
"""
        for sig, expected in [("TERM", 124), ("INT", 130)]:
            record = root / ("guard-" + sig)
            command = [sys.executable, str(Path(runner.__file__).resolve()), "--timeout", "10", "--",
                       sys.executable, str(fixture), str(record), "hang"]
            if sig == "TERM":
                command = ["timeout", "--kill-after=3s", "0.7s", *command]
            assert runner.run_owned([sys.executable, "-c", guard, str(record), *command], cwd=root,
                                    env={**os.environ, "PI_BIN": str(fake_pi), "EXPECTED_STATUS": str(expected)}, timeout=6) == 0
            assert_gone(record)
            count += 1
    print(f"isolated-check self-test: {count} checks passed in {time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_runner", sys.argv[1])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    self_test(runner)
