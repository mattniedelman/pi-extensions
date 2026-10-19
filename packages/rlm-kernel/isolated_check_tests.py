"""Tiny executable fixtures for isolated-check.py; no model or Monty probes."""
import json
import os
from pathlib import Path
import signal
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


def ownership_probe(runner):
    """The spec-review discriminator, with backup cleanup only for this fixture."""
    with tempfile.TemporaryDirectory(prefix="pi-vmk-spec-ownership-") as directory:
        root = Path(directory)
        record, gate = root / "child", root / "gate"
        child_code = "import os,signal,time; from pathlib import Path; signal.signal(signal.SIGTERM,signal.SIG_IGN); Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(record)
        parent_code = "import subprocess,sys,time; from pathlib import Path; subprocess.Popen([sys.executable,'-c',%r],start_new_session=True); gate=Path(%r);\nwhile not gate.exists(): time.sleep(.01)" % (child_code, str(gate))
        with runner.subreaper():
            unrelated = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)
            child = None
            identity = None
            try:
                deadline = time.monotonic() + 2
                while not record.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                assert record.exists()
                child = int(record.read_text())
                identity = runner.process_info(child)[0]
                owned_code = "from pathlib import Path; import time; Path(%r).touch(); time.sleep(.3)" % str(gate)
                status = runner.run_owned([sys.executable, "-c", owned_code], cwd=root, env=dict(os.environ), timeout=2)
                unrelated.wait(timeout=2)
                info = runner.process_info(child)
                alive = (info is not None and info[0] == identity and
                         Path(f"/proc/{child}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z")
                print("OWNERSHIP: owned command status=%s; unrelated adopted grandchild survives=%s (required True)" % (status, alive), flush=True)
                assert status == 0, status
                assert alive, "runner killed a grandchild of an excluded, pre-existing unrelated process"
                assert info[1] == os.getpid(), "fixture did not exercise adoption"
            finally:
                if unrelated.poll() is None:
                    unrelated.kill()
                unrelated.wait(timeout=2)
                info = runner.process_info(child) if child else None
                if info and info[0] == identity:
                    os.kill(child, signal.SIGKILL)
                    try:
                        os.waitpid(child, 0)
                    except ChildProcessError:
                        pass


def dynamic_ownership_probe(runner):
    """An excluded parent forks only after the owned command has started."""
    with tempfile.TemporaryDirectory(prefix="pi-vmk-dynamic-ownership-") as directory:
        root = Path(directory)
        ready, gate, record = root / "ready", root / "gate", root / "child"
        child_code = """
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
identity = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
Path(%r).write_text(str(os.getpid()) + ' ' + identity)
time.sleep(30)
""" % str(record)
        parent_code = """
import subprocess, sys, time
from pathlib import Path
Path(%r).touch()
while not Path(%r).exists(): time.sleep(.005)
subprocess.Popen([sys.executable, '-c', %r], start_new_session=True)
while not Path(%r).exists(): time.sleep(.005)
""" % (str(ready), str(gate), child_code, str(record))
        with runner.subreaper():
            unrelated = subprocess.Popen([sys.executable, "-c", parent_code], start_new_session=True)
            try:
                deadline = time.monotonic() + 2
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.005)
                assert ready.exists() and not record.exists()
                owned_code = "from pathlib import Path; import time; Path(%r).touch(); time.sleep(.3)" % str(gate)
                status = runner.run_owned([sys.executable, "-c", owned_code], cwd=root, env=dict(os.environ), timeout=2)
                unrelated.wait(timeout=2)
                pid, identity = record.read_text().split()
                info = runner.process_info(int(pid))
                assert status == 0 and info and info[0] == identity and info[1] == os.getpid(), (status, info)
                assert Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z", "external grandchild was killed but not reaped"
                print("OWNERSHIP: dynamically forked unrelated grandchild survives adoption", flush=True)
            finally:
                if unrelated.poll() is None:
                    unrelated.kill()
                unrelated.wait(timeout=2)
                if record.exists():
                    pid, identity = record.read_text().split()
                    info = runner.process_info(int(pid))
                    if info and info[0] == identity:
                        os.kill(int(pid), signal.SIGKILL)
                        try:
                            os.waitpid(int(pid), 0)
                        except ChildProcessError:
                            pass


def real_pi_probe(runner):
    """Maintain the review's real, offline poison-discovery discriminator."""
    with tempfile.TemporaryDirectory(prefix="pi-vmk-real-pi-") as directory:
        root = Path(directory)
        caller, agent = root / "caller", root / "personal-agent"
        caller.mkdir()
        (caller / ".pi" / "extensions").mkdir(parents=True)
        (agent / "extensions").mkdir(parents=True)
        markers = [root / "local-loaded", root / "global-loaded"]
        for extension, marker in zip([caller / ".pi/extensions/poison.ts", agent / "extensions/poison.ts"], markers):
            extension.write_text("import {writeFileSync} from 'node:fs'; writeFileSync(%s,'loaded'); export default function(pi){}\n" % json.dumps(str(marker)))
        (caller / "AGENTS.md").write_text("SPEC_POISON_CONTEXT_MUST_NOT_LOAD\n")
        report = root / "report.json"
        extension = root / "explicit.ts"
        extension.write_text("import {writeFileSync} from 'node:fs'; export default function(pi) { pi.registerCommand('spec-isolation-probe', {description:'offline spec probe', handler:async (_args,ctx)=>writeFileSync(%s,JSON.stringify({cwd:ctx.cwd,agent:process.env.PI_CODING_AGENT_DIR,state:process.env.PI_RLM_STATE_DIR,sessions:process.env.PI_CODING_AGENT_SESSION_DIR}))}); }\n" % json.dumps(str(report)))
        source = {**os.environ, "PI_CODING_AGENT_DIR": str(agent), "PI_SESSION_FILE": "must-not-forward",
                  "NODE_OPTIONS": "--require must-not-load"}
        with runner.isolated_environment(source) as env:
            private = Path(env["PI_CODING_AGENT_DIR"]).parent
            command = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--extension", str(extension)]
            supervisor = r"""
import json, subprocess, sys, time
from pathlib import Path
proc = subprocess.Popen(sys.argv[2:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
try:
    proc.stdin.write(json.dumps({'id':'probe','type':'prompt','message':'/spec-isolation-probe'})+'\n'); proc.stdin.flush()
    deadline = time.monotonic() + 4
    while not Path(sys.argv[1]).exists() and proc.poll() is None and time.monotonic() < deadline: time.sleep(.01)
    assert Path(sys.argv[1]).exists(), 'real pi explicit fixture never reported'
finally:
    proc.terminate()
    assert proc.wait(timeout=3) == 143
"""
            status = runner.run_owned([sys.executable, "-c", supervisor, str(report), *command], cwd=caller, env=env, timeout=8)
            assert status == 0, status
            data = json.loads(report.read_text())
            assert data["cwd"] == str(private / "cwd"), data
            for key, envkey in [("agent", "PI_CODING_AGENT_DIR"), ("state", "PI_RLM_STATE_DIR"), ("sessions", "PI_CODING_AGENT_SESSION_DIR")]:
                assert data[key] == env[envkey], data
            assert not any(marker.exists() for marker in markers), "real discovered extension ran"
        assert not private.exists()
        print("REAL PI: explicit offline command ran in private cwd/agent/state/sessions; no local/global poison loaded; private root removed", flush=True)


SAFETY_TREE = r"""
import json, os, signal, subprocess, sys, threading, time
from pathlib import Path
root, mode = Path(sys.argv[1]), sys.argv[2]
def record(name):
    identity = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
    (root / (name + '.json')).write_text(json.dumps({'pid': os.getpid(), 'identity': identity,
        'private': str(Path(os.environ['PI_CODING_AGENT_DIR']).parent)}))
def wait_for(name):
    deadline = time.monotonic() + 2
    while not (root / name).exists():
        assert time.monotonic() < deadline, name
        time.sleep(.002)
if mode == 'child':
    def term(*_):
        (root / 'cleaning').touch()
        # Thread coverage requires TERM discovery before the parent is killed
        # and adoption could hide a missing non-main-thread children walk.
        if (root / 'root.json').exists():
            parent = json.loads((root / 'root.json').read_text())
            if os.getppid() == parent['pid']:
                (root / 'thread-term').touch()
    signal.signal(signal.SIGTERM, term)
    record('child')
    time.sleep(30)
elif mode == 'termfork':
    fired = False
    def term(*_):
        global fired
        if fired: return
        fired = True
        if os.fork() == 0:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.setsid()
            record('fork1')
            if os.fork(): os._exit(0)
            os.setsid()
            record('fork2')
            time.sleep(30)
            os._exit(0)
        wait_for('fork2.json')
    signal.signal(signal.SIGTERM, term)
    record('root')
    time.sleep(30)
else:
    def spawn():
        subprocess.Popen([sys.executable, __file__, str(root), 'child'], start_new_session=True)
        if mode == 'thread': time.sleep(30)
    if mode == 'thread':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        threading.Thread(target=spawn).start()
    else:
        spawn()
    wait_for('child.json')
    record('root')
    if mode == 'thread': time.sleep(30)
    sys.exit(125 if mode == 'exit125' else 0)
"""


SAFETY_GUARD = r"""
import importlib.util, json, os, signal, subprocess, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
root, case, target, number = Path(sys.argv[2]), sys.argv[3], sys.argv[4], int(sys.argv[5])
fixture = root / 'safety-tree.py'
mode = case if case in ('thread', 'termfork', 'exit125') else 'success'
command = [sys.executable, str(fixture), str(root), mode]
if case == 'cleanup125':
    # Inject a teardown error only after real identity-safe cleanup. This proves
    # error precedence without leaving survivors for the enclosing backup.
    launcher = r'''
import importlib.util, os, sys, time
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
cleanup = runner.OwnedProcesses.cleanup
def fail_after_cleanup(self):
    cleanup(self)
    raise RuntimeError('deliberate cleanup failure')
runner.OwnedProcesses.cleanup = fail_after_cleanup
try:
    with runner.isolated_environment() as env:
        os.environ.update(env)
        status = runner.supervise(sys.argv[2:], timeout=3, deadline=time.monotonic()+3)
    sys.exit(status)
except (OSError, RuntimeError) as error:
    print(error, file=sys.stderr)
    sys.exit(125)
'''
    cli = [sys.executable, '-c', launcher, runner.__file__, *command]
else:
    cli = [sys.executable, runner.__file__, '--timeout', '3', '--', *command]
proc = subprocess.Popen(cli)
marker = 'root.json' if case in ('thread', 'termfork') else 'cleaning'
deadline = time.monotonic() + 3
while not (root / marker).exists() and proc.poll() is None and time.monotonic() < deadline:
    time.sleep(.002)
assert (root / marker).exists(), ('fixture never ready', case, proc.poll())
pid = proc.pid
if target == 'supervisor':
    supervisors = runner.children(pid)
    assert len(supervisors) == 1, supervisors
    pid = supervisors.pop()
# Late cases are gated by TERM from cleanup, after the command has exited.
os.kill(pid, number)
status = proc.wait(timeout=3)
records = [json.loads(path.read_text()) for path in root.glob('*.json')]
assert len(records) == (3 if case == 'termfork' else 2), records
for data in records:
    info = runner.process_info(data['pid'])
    assert not (info and info[0] == data['identity']), ('owned identity survived BEFORE backup', data)
    assert not Path(data['private']).exists(), ('private directory survived BEFORE backup', data)
if case == 'thread':
    assert (root / 'thread-term').exists(), 'non-main-thread child missed during TERM before adoption'
expected = 125 if case in ('exit125', 'cleanup125') else 128 + number
print(f'SAFETY {case}/{target}/{signal.Signals(number).name}: status={status}, expected={expected}; '
      f'{len(records)} owned identities absent and private directory removed BEFORE backup', flush=True)
assert status == expected, (case, target, status, expected)
"""


def safety_probes(runner, root, fake_pi):
    failures = []
    cases = [(case, 'caller', signal.SIGTERM) for case in ('thread', 'termfork')]
    cases += [('late', target, sig) for target in ('caller', 'supervisor')
              for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)]
    cases += [(case, 'caller', signal.SIGTERM) for case in ('exit125', 'cleanup125')]
    for index, (case, target, sig) in enumerate(cases):
        directory = root / ('safety-' + str(index))
        directory.mkdir()
        (directory / 'safety-tree.py').write_text(SAFETY_TREE)
        # A dedicated outer supervisor provides bounded fixture-only backup on
        # assertion/readiness failure. The guard proves absence BEFORE it exits.
        status = runner.run_owned([sys.executable, '-c', SAFETY_GUARD, runner.__file__,
                                  str(directory), case, target, str(int(sig))], cwd=root,
                                 env={**os.environ, 'PI_BIN': str(fake_pi)}, timeout=5)
        if status != 0:
            failures.append((case, target, sig.name, status))
    assert not failures, failures
    return len(cases)


def self_test(runner):
    count = 0
    started = time.monotonic()
    ownership_probe(runner)
    dynamic_ownership_probe(runner)
    real_pi_probe(runner)
    count += 3
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
if os.environ['EXPECTED_STATUS'] in ('130', '129'):
    deadline = time.monotonic() + 3
    while not Path(str(record) + '.env').exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert Path(str(record) + '.env').exists(), 'fixture never became ready'
    os.kill(proc.pid, signal.SIGINT if os.environ['EXPECTED_STATUS'] == '130' else signal.SIGHUP)
status = proc.wait(timeout=5)
assert status == int(os.environ['EXPECTED_STATUS']), status
pids = [int(record.read_text()), int(Path(str(record) + '.child').read_text())]
assert all(not Path('/proc/' + str(pid)).exists() for pid in pids), pids
assert not Path(Path(str(record) + '.env').read_text()).parent.exists(), 'runner private directories survived'
"""
        for sig, expected in [("TERM", 124), ("INT", 130), ("HUP", 129)]:
            record = root / ("guard-" + sig)
            command = [sys.executable, str(Path(runner.__file__).resolve()), "--timeout", "10", "--",
                       sys.executable, str(fixture), str(record), "hang"]
            if sig == "TERM":
                command = ["timeout", "--kill-after=3s", "0.7s", *command]
            assert runner.run_owned([sys.executable, "-c", guard, str(record), *command], cwd=root,
                                    env={**os.environ, "PI_BIN": str(fake_pi), "EXPECTED_STATUS": str(expected)}, timeout=6) == 0
            assert_gone(record)
            count += 1
        count += safety_probes(runner, root, fake_pi)
    print(f"isolated-check self-test: {count} checks passed in {time.monotonic() - started:.2f}s", flush=True)


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_runner", sys.argv[1])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    self_test(runner)
