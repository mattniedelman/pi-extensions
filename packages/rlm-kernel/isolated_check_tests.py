"""Tiny executable fixtures for isolated-check.py; no model or Monty probes."""
import json
import math
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


CALLER_EXIT_GUARD = r"""
import importlib.util, os, signal, subprocess, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
observed = []
class ExitGate(subprocess.Popen):
    def __init__(self, *args, **kwargs):
        self.signaled = False
        super().__init__(*args, **kwargs)
        observed.append((self.pid, runner.process_info(self.pid)[0]))
    def poll(self):
        status = super().poll()
        if status is not None and not self.signaled:
            self.signaled = True
            # Deliver only AFTER the real supervisor exits: forwarding can no
            # longer affect it. Do not fabricate a poll result or command status.
            os.kill(os.getpid(), signal.SIGTERM)
        return status
runner.subprocess.Popen = ExitGate
command_status = int(sys.argv[2])
with runner.isolated_environment() as env:
    private = Path(env['PI_CODING_AGENT_DIR']).parent
    status = runner.run_owned([sys.executable, '-c', 'raise SystemExit(%s)' % command_status],
                              cwd=Path.cwd(), env=env, timeout=2)
assert len(observed) == 1, observed
for pid, identity in observed:
    info = runner.process_info(pid)
    assert not (info and info[0] == identity), 'supervisor identity survived BEFORE backup'
assert not private.exists(), 'private directory survived BEFORE backup'
expected = 125 if command_status == 125 else 143
print(f'CALLER EXIT: command={command_status}, status={status}, expected={expected}; '
      'supervisor identity absent and private directory removed BEFORE backup', flush=True)
assert status == expected, (status, expected)
"""


def capture_probes(runner):
    """File-backed streams must retain output without pipe/deadline deadlocks."""
    with tempfile.TemporaryDirectory(prefix="pi-owned-capture-") as directory:
        root = Path(directory)
        command = [sys.executable, "-c", "import sys; data=sys.stdin.read(); "
                   "sys.stdout.write(data+'o'*80000); sys.stderr.write('e'*80000)"]
        result = runner.run_owned_capture(command, cwd=root, env=dict(os.environ),
                                          timeout=2, input="tiny\n\U0001f600\n")
        assert result.returncode == 0, result.stderr
        assert result.stdout == "tiny\n\U0001f600\n" + "o" * 80000
        assert result.stderr.startswith("e" * 80000) and "0 survivors" in result.stderr
        count = 1
        for mode, expected in (("success", 0), ("failure", 1), ("eof", 0), ("hang", 124)):
            record = root / mode
            started = time.monotonic()
            result = runner.run_owned_capture([sys.executable, "-c", TREE, str(record), mode],
                                              cwd=root, env=dict(os.environ), timeout=.5 if mode == "hang" else 2)
            assert result.returncode == expected, (mode, result)
            assert time.monotonic() - started < 3
            assert_gone(record)  # before the self-test's enclosing backup
            assert "0 survivors" in result.stderr
            if mode == "hang":
                assert "deadline exceeded" in result.stderr
            count += 1
        result = runner.run_owned_capture([sys.executable, "-c", "raise SystemExit(125)"],
                                          cwd=root, env=dict(os.environ), timeout=2)
        assert result.returncode == 125 and "0 survivors" in result.stderr
        count += 1
    print(f"isolated-check capture: {count} checks passed (absence BEFORE backup)", flush=True)
    return count


ACTIVE_REAP = r"""
import importlib.util, json, os, signal, subprocess, sys, time
from pathlib import Path
spec = importlib.util.spec_from_file_location('runner', sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
record = Path(sys.argv[2])
child_code = '''
import json, os, time
from pathlib import Path
for fd in (0, 1, 2): os.close(fd)
identity = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
record = Path(%r)
record.with_suffix('.tmp').write_text(json.dumps({'pid': os.getpid(), 'start': identity}))
record.with_suffix('.tmp').replace(record)
time.sleep(30)
''' % str(record)
parent_code = '''
import subprocess, sys, threading, time
def spawn():
    subprocess.Popen([sys.executable, '-c', %r], start_new_session=True)
    time.sleep(30)
threading.Thread(target=spawn).start()
time.sleep(30)
''' % child_code
parent = subprocess.Popen([sys.executable, '-c', parent_code])
deadline = time.monotonic() + 2
while not record.exists():
    assert time.monotonic() < deadline, 'escaped child never ready'
    time.sleep(.002)
child = json.loads(record.read_text())
pid, start = child['pid'], child['start']
assert runner.process_info(pid) == (start, parent.pid)
main_children = Path(f'/proc/{parent.pid}/task/{parent.pid}/children').read_text().split()
assert str(pid) not in main_children, 'fixture did not use a creator thread'
assert pid in runner.children(parent.pid), 'missing non-main-thread edge'
assert os.getsid(pid) == pid and os.getpgid(pid) == pid
assert all(not Path(f'/proc/{pid}/fd/{fd}').exists() for fd in (0, 1, 2))
# Only the fixture parent is killed here; the child must remain alive on adoption.
parent.kill()
parent.wait(timeout=1)
assert runner.process_info(pid) == (start, os.getppid()), 'not adopted by dedicated supervisor'
assert Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z', 'child must outlive parent'
os.kill(pid, signal.SIGKILL)
deadline = time.monotonic() + 1
while (info := runner.process_info(pid)) and info[0] == start:
    assert time.monotonic() < deadline, ('active supervisor did not reap owned adoptee', pid, info)
    time.sleep(.002)
print('ACTIVE REAP: escaped thread/closed-pipes identity absent BEFORE root exit/backup', flush=True)
raise SystemExit(7)
"""


def active_reaping_probe(runner):
    with tempfile.TemporaryDirectory(prefix="pi-active-reap-") as directory:
        root = Path(directory)
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        identity = runner.process_info(sentinel.pid)[0]
        try:
            result = runner.run_owned_capture([sys.executable, "-c", ACTIVE_REAP, runner.__file__,
                                               str(root / "child.json")], cwd=root,
                                              env=dict(os.environ), timeout=4)
            assert sentinel.poll() is None and runner.process_info(sentinel.pid)[0] == identity
            assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
            assert "absent BEFORE root exit/backup" in result.stdout
            print(result.stdout, end="", flush=True)
            print("ACTIVE REAP: root Popen status 7 preserved; unrelated sentinel alive", flush=True)
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=1)
    assert not root.exists()
    print("isolated-check active-reaping: 1 check passed", flush=True)


OUTER_SIGNAL_GUARD = """
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


def environment_case(runner, root, env, private, _parameter, source):
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


def wrapper_case(runner, root, env, private, discover, source):
    if discover is None:
        pi_report = root / "pi.json"
        assert runner.run_owned(["pi", "--extension", "/explicit/index.ts", str(pi_report)], cwd=root, env=env, timeout=2) == 0
        result = json.loads(pi_report.read_text())
        assert Path(result["cwd"]).is_relative_to(private) and result["cwd"] != str(root)
        for flag in ["--no-extensions", "--no-context-files", "--no-mcp", "--no-skills", "--no-prompt-templates", "--no-themes", "--offline"]:
            assert flag in result["argv"]
        assert result["argv"][-2:] == ["--extension", "/explicit/index.ts"]
        assert result["env"]["PI_CODING_AGENT_DIR"] == env["PI_CODING_AGENT_DIR"]
    else:
        with runner.isolated_environment(env, discover_extensions=discover) as nested:
            nested_root = Path(nested["PI_CODING_AGENT_DIR"]).parent
            nested_report = root / f"nested-{discover}.json"
            assert runner.run_owned([nested["PI_BIN"], str(nested_report)], cwd=root, env=nested, timeout=2) == 0
            data = json.loads(nested_report.read_text())
            assert data["cwd"] == str(nested_root / "cwd")
            assert ("--no-extensions" in data["argv"]) is (not discover), data
            assert data["env"]["PI_CODING_AGENT_DIR"] == nested["PI_CODING_AGENT_DIR"]
            assert nested_root != private
        assert not nested_root.exists()


def probe_case(runner, root, env, private, probe, source):
    {"ownership": ownership_probe, "dynamic": dynamic_ownership_probe,
     "real-pi": real_pi_probe}[probe](runner)


def tree_case(runner, root, env, private, mode, source):
    expected = {"success": 0, "exit7": 7, "failure": 1, "eof": 0, "hang": 124}[mode]
    record = root / mode
    before = time.monotonic()
    status = runner.run_owned([sys.executable, "-c", TREE, str(record), mode],
                             cwd=root, env=env, timeout=.5 if mode == "hang" else 2)
    assert status == expected, (mode, status)
    assert time.monotonic() - before < 3, mode
    assert_gone(record)


def missing_case(runner, root, env, private, parameter, source):
    assert runner.run_owned([str(root / "missing-command")], cwd=root, env=env, timeout=1) == 127


def removal_case(runner, root, env, private, parameter, source):
    # Removal and poison equality are asserted after leaving the private scope.
    assert private.exists()


def outer_signal_case(runner, root, env, private, sig, source):
    expected = {"TERM": 124, "INT": 130, "HUP": 129}[sig]
    fixture = root / "tree.py"
    fixture.write_text(TREE)
    record = root / ("guard-" + sig)
    command = [sys.executable, str(Path(runner.__file__).resolve()), "--timeout", "10", "--",
               sys.executable, str(fixture), str(record), "hang"]
    if sig == "TERM":
        command = ["timeout", "--kill-after=3s", "0.7s", *command]
    assert runner.run_owned([sys.executable, "-c", OUTER_SIGNAL_GUARD, str(record), *command],
                            cwd=root, env={**env, "EXPECTED_STATUS": str(expected)}, timeout=6) == 0
    assert_gone(record)


def safety_case(runner, root, env, private, parameter, source):
    case, target, sig = parameter
    directory = root / "safety"
    directory.mkdir()
    (directory / "safety-tree.py").write_text(SAFETY_TREE)
    assert runner.run_owned([sys.executable, "-c", SAFETY_GUARD, runner.__file__,
                            str(directory), case, target, str(int(sig))],
                           cwd=root, env=env, timeout=5) == 0, parameter


def caller_exit_case(runner, root, env, private, command_status, source):
    assert runner.run_owned([sys.executable, "-c", CALLER_EXIT_GUARD, runner.__file__,
                            str(command_status)], cwd=root, env=env, timeout=3) == 0


# Fixed original logical inventory, independently repeated in dispatch controls.
ORIGINAL_NAMES = (
    "unrelated-adoption", "dynamic-unrelated-adoption", "real-pi-poison-isolation",
    "environment-caller-cwd", "explicit-wrapper", "nested-explicit-wrapper",
    "nested-discovery-wrapper", "tree-success", "tree-exit7", "tree-failure",
    "tree-eof", "tree-hang", "missing-command", "private-root-poison-unchanged",
    "outer-TERM", "outer-INT", "outer-HUP", "safety-thread", "safety-termfork",
    "late-caller-TERM", "late-caller-INT", "late-caller-HUP",
    "late-supervisor-TERM", "late-supervisor-INT", "late-supervisor-HUP",
    "exit125-precedence", "cleanup125-precedence", "caller-after-exit0", "caller-after-exit125",
)
# Budgets cover each whole logical case, not individual assertions: original
# operation guard + its existing 3s cleanup allowance + 1s fixture setup
# + the original 2s unrelated-sentinel teardown guard.
# Ownership additionally allows 2s readiness, 2s unrelated wait and 2s probe teardown.
# Each dispatcher run_owned adds its existing 3s allowance; suite setup adds 1s.
CLEANUP_ALLOWANCE = 3
SETUP_ALLOWANCE = 1
CASES = (
    ("unrelated-adoption", 14, probe_case, "ownership"),
    ("dynamic-unrelated-adoption", 14, probe_case, "dynamic"),
    ("real-pi-poison-isolation", 14, probe_case, "real-pi"),
    ("environment-caller-cwd", 8, environment_case, None),
    ("explicit-wrapper", 8, wrapper_case, None),
    ("nested-explicit-wrapper", 8, wrapper_case, False),
    ("nested-discovery-wrapper", 8, wrapper_case, True),
    ("tree-success", 8, tree_case, "success"),
    ("tree-exit7", 8, tree_case, "exit7"),
    ("tree-failure", 8, tree_case, "failure"),
    ("tree-eof", 8, tree_case, "eof"),
    ("tree-hang", 7, tree_case, "hang"),
    ("missing-command", 7, missing_case, None),
    ("private-root-poison-unchanged", 3, removal_case, None),
    ("outer-TERM", 12, outer_signal_case, "TERM"),
    ("outer-INT", 12, outer_signal_case, "INT"),
    ("outer-HUP", 12, outer_signal_case, "HUP"),
    ("safety-thread", 11, safety_case, ("thread", "caller", signal.SIGTERM)),
    ("safety-termfork", 11, safety_case, ("termfork", "caller", signal.SIGTERM)),
    ("late-caller-TERM", 11, safety_case, ("late", "caller", signal.SIGTERM)),
    ("late-caller-INT", 11, safety_case, ("late", "caller", signal.SIGINT)),
    ("late-caller-HUP", 11, safety_case, ("late", "caller", signal.SIGHUP)),
    ("late-supervisor-TERM", 11, safety_case, ("late", "supervisor", signal.SIGTERM)),
    ("late-supervisor-INT", 11, safety_case, ("late", "supervisor", signal.SIGINT)),
    ("late-supervisor-HUP", 11, safety_case, ("late", "supervisor", signal.SIGHUP)),
    ("exit125-precedence", 11, safety_case, ("exit125", "caller", signal.SIGTERM)),
    ("cleanup125-precedence", 11, safety_case, ("cleanup125", "caller", signal.SIGTERM)),
    ("caller-after-exit0", 9, caller_exit_case, 0),
    ("caller-after-exit125", 9, caller_exit_case, 125),
)


def validate_registry(cases=None):
    cases = CASES if cases is None else cases
    names = [case[0] for case in cases]
    if len(names) != len(set(names)):
        raise ValueError("duplicate scenario registration")
    if set(names) != set(ORIGINAL_NAMES):
        raise ValueError(f"original inventory mismatch: missing={sorted(set(ORIGINAL_NAMES) - set(names))}; "
                         f"unknown={sorted(set(names) - set(ORIGINAL_NAMES))}")
    if any(not math.isfinite(case[1]) or case[1] <= 0 or not callable(case[2]) for case in cases):
        raise ValueError("invalid scenario handler/budget")
    return {case[0]: case for case in cases}


def select_cases(names=None):
    registry = validate_registry()
    if names is None:
        names = ORIGINAL_NAMES
    if not names or len(names) != len(set(names)):
        raise ValueError("empty or duplicate scenario selection")
    unknown = set(names) - set(registry)
    if unknown:
        raise ValueError(f"unknown scenarios: {sorted(unknown)}")
    return tuple(registry[name] for name in names)


def total_budget(selected):
    return math.ceil(sum(case[1] + CLEANUP_ALLOWANCE for case in selected) + SETUP_ALLOWANCE)


def case_command(runner, name, envelope):
    return [sys.executable, str(Path(__file__).resolve()), runner.__file__,
            "--case-oracle", name, str(envelope)]


def run_case(runner, name, envelope):
    case = select_cases([name])[0]
    pid, identity = os.getpid(), runner.process_info(os.getpid())[0]
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="case-", dir=envelope) as directory:
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
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        sentinel_identity = runner.process_info(sentinel.pid)[0]
        try:
            with runner.isolated_environment(source) as env:
                private = Path(env["PI_CODING_AGENT_DIR"]).parent
                case[2](runner, root, env, private, case[3], source)
                assert sentinel.poll() is None and runner.process_info(sentinel.pid)[0] == sentinel_identity, "cleanup touched unrelated sentinel"
            assert not private.exists(), "private directories survived BEFORE backup"
            assert (poison / "settings.json").read_bytes() == original
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=2)
    assert not root.exists(), "case fixture survived BEFORE backup"
    # Receipt is emitted only after original assertions and fixture teardown;
    # the dispatcher also requires successful exit and supervisor cleanup.
    (Path(envelope) / "receipt.json").write_text(json.dumps({
        "name": name, "pid": pid, "identity": identity, "fixture": str(root),
        "fixtureCleanup": True, "runtimeSeconds": round(time.monotonic() - started, 3)}))


def execute_case(runner, case):
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="pi-self-case-") as directory:
        envelope = Path(directory)
        # Children create even auxiliary probe fixtures under the private scope.
        env = {**os.environ, "TMPDIR": directory}
        try:
            status = runner.run_owned(case_command(runner, case[0], envelope),
                                     cwd=Path.cwd(), env=env, timeout=case[1])
            receipt = envelope / "receipt.json"
            record = json.loads(receipt.read_text()) if receipt.exists() else {"name": case[0], "fixtureCleanup": False}
        except (OSError, RuntimeError, ValueError) as error:
            print(f"FAIL {case[0]}: supervision/receipt error: {error}", file=sys.stderr, flush=True)
            status, record = 125, {"name": case[0], "fixtureCleanup": False}
        if status == 0:
            info = runner.process_info(record.get("pid", os.getpid()))
            valid = (record.get("name") == case[0] and record.get("fixtureCleanup") is True
                     and "fixture" in record and not Path(record["fixture"]).exists()
                     and not (info and info[0] == record.get("identity")))
            if not valid:
                status = 125
        record.update(status=status, supervisorRuntimeSeconds=round(time.monotonic() - started, 3))
    print("CASE_RESULT " + json.dumps(record, sort_keys=True), flush=True)
    return record


def check_results(selected, records, *, full):
    expected = [case[0] for case in selected]
    actual = [record["name"] for record in records if record is not None]
    if actual != expected or len(actual) != len(set(actual)):
        raise ValueError(f"incomplete execution ledger: expected={expected}, actual={actual}")
    if full and (len(actual) != 29 or set(actual) != set(ORIGINAL_NAMES)):
        raise ValueError("full PASS requires all 29 original scenarios exactly once")
    if any(record["status"] != 0 for record in records):
        raise ValueError("nonzero scenario execution in ledger")
    if (any(record.get("fixtureCleanup") is not True for record in records)
            or len({record.get("fixture") for record in records}) != len(records)
            or len({(record.get("pid"), record.get("identity")) for record in records}) != len(records)):
        raise ValueError("missing cleanup evidence or reused fixture/process identity")


def self_test(runner, names=None):
    selected = select_cases(names)
    records = []
    started = time.monotonic()
    for index, case in enumerate(selected):
        record = execute_case(runner, case)
        records.append(record)
        if record is None or record["status"] != 0:
            print(f"FAIL {case[0]}: status={record['status'] if record else 'missing receipt'}; "
                  f"remaining not run={[item[0] for item in selected[index+1:]]}", file=sys.stderr, flush=True)
            return record["status"] if record else 125
    try:
        check_results(selected, records, full=names is None)
    except ValueError as error:
        print(f"FAIL self-test: {error}", file=sys.stderr, flush=True)
        return 125
    label = "29/29 original scenarios passed" if names is None else f"{len(records)} selected scenarios passed (not full-suite PASS)"
    print(f"isolated-check self-test: {label} in {time.monotonic() - started:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    import importlib.util
    spec = importlib.util.spec_from_file_location("isolated_runner", sys.argv[1])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    if sys.argv[2:] == ["--capture-only"]:
        # Separate bounded backup scope; recovery callers retain this contract.
        sys.exit(runner.run_owned([sys.executable, str(Path(__file__).resolve()),
                                  str(Path(runner.__file__).resolve()), "--capture-oracle"],
                                 cwd=Path.cwd(), env=dict(os.environ), timeout=7))
    elif sys.argv[2:] == ["--capture-oracle"]:
        capture_probes(runner)
    elif sys.argv[2:] == ["--active-only"]:
        sys.exit(runner.run_owned([sys.executable, str(Path(__file__).resolve()),
                                  str(Path(runner.__file__).resolve()), "--active-oracle"],
                                 cwd=Path.cwd(), env=dict(os.environ), timeout=6))
    elif sys.argv[2:] == ["--active-oracle"]:
        active_reaping_probe(runner)
    elif len(sys.argv) == 5 and sys.argv[2] == "--case-oracle":
        run_case(runner, sys.argv[3], sys.argv[4])
    else:
        sys.exit(self_test(runner, sys.argv[3:] if sys.argv[2:3] == ["--selected"] else None))
