#!/usr/bin/env python3
"""Credential-free synthetic guard proof, NOT actual pi live-policy proof."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import permissions_tests as controls

p = controls.permissions
ROOT = Path(__file__).resolve().parent

# Only fixture commands run here. The marker is not the real runtime sidecar.
SIDECAR = "import signal\nprint('ready', flush=True)\nsignal.pause()\n"
NODE = r"""
import factory from MODULE_URI;
import {readFileSync, writeFileSync} from 'node:fs';
const handlers = {};
factory({on: (name, handler) => {handlers[name] = handler;}});
const events = JSON.parse(readFileSync('events.json', 'utf8'));
const starts = events.filter(e => e.type === 'tool_execution_start');
const parent = starts.find(e => e.toolName === 'python');
const call = e => handlers.tool_call({toolName:e.toolName, toolCallId:e.toolCallId, input:e.args});
const result = (e, input) => handlers.tool_result({toolName:e.toolName, toolCallId:e.toolCallId,
  input, isError:e.isError});
if (call(parent) !== undefined) throw Error('synthetic python blocked');
const allowed = starts.find(e => e.args.path === 'allowed.txt');
const denied = starts.find(e => e.args.path === 'denied.txt');
if (call(allowed) !== undefined) throw Error('synthetic allowed blocked');
const content = readFileSync('allowed.txt', 'utf8');
if (content !== 'IUA_ALLOWED\n') throw Error('synthetic allowed content');
const decision = call(denied);
if (decision?.block !== true || decision.reason !== 'IUA_DENIED') throw Error('synthetic denial failed');
// A blocked call is not executed and does not produce a tool_result hook.
const ends = events.filter(e => e.type === 'tool_execution_end');
result(ends.find(e => e.toolCallId === allowed.toolCallId), allowed.args);
result(ends.find(e => e.toolCallId === parent.toolCallId), parent.args);
writeFileSync('host-evidence.json', JSON.stringify({allowedRead:true, deniedBlocked:true,
  eventsAfterRead:true}), {mode:0o600});
"""
HOST = r"""#!PYTHON
import json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, ROOT_LITERAL)
import permissions_tests as controls
p = controls.permissions
rpc, _policy, _phases = controls.DiagnosticControls().fixture()
scenario = os.environ['ASSEMBLY_CASE']
if scenario == 'oldOrder':
    rpc.events.insert(0, rpc.events.pop())
if scenario == 'sensorAndArgs':
    next(e for e in rpc.events if e['type'] == 'tool_execution_start' and e['toolName'] == 'python')['args'] = {'code':'synthetic-private-input'}
sidecar = subprocess.Popen([sys.executable, os.environ['ASSEMBLY_SIDECAR']],
    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
assert sidecar.stdout.readline() == 'ready\n', 'synthetic sidecar readiness'
sidecar.stdout.close()
info = p.isolation.process_info(sidecar.pid)
Path('sidecar-identity.json').write_text(json.dumps([sidecar.pid, info[0]]))
def emit(value):
    print(json.dumps(value), flush=True)
try:
    for line in sys.stdin:
        request = json.loads(line)
        if request['type'] == 'get_state':
            emit({'type':'response', 'id':request['id'], 'command':'get_state', 'success':True,
                  'data':{'model':{'provider':'litellm', 'id':'fixture'}}})
        elif request['type'] == 'prompt':
            emit({'type':'response', 'id':request['id'], 'command':'prompt', 'success':True,
                  'data':{'disposition':'started'}})
            Path('events.json').write_text(json.dumps(rpc.events))
            subprocess.run(['node', 'node-host.mjs'], check=True, timeout=3)
            if scenario in ('sensorAndArgs', 'decodedLeak', 'stderrLeak'):
                secret = Path('denied.txt').read_text().strip()
                if scenario == 'decodedLeak':
                    record = json.dumps({'type':'extension_error', 'headers':{'private':secret}})
                    print(record.replace(secret, '\\u%04x' % ord(secret[0]) + secret[1:]), flush=True)
                if scenario == 'stderrLeak':
                    print(secret, file=sys.stderr, flush=True)
            # Like the review probe, both old and corrected ordering emit ONLY after IO.
            for event in rpc.events:
                emit(event)
    # EOF from live_check is the deterministic final-shutdown boundary.
    if scenario == 'finalDenied':
        Path('denied.txt').read_bytes()
    if scenario == 'malformedFinal':
        print('{invalid', flush=True)
finally:
    if scenario != 'survivor':
        sidecar.terminate()
        sidecar.wait(timeout=2)
sys.exit(3 if scenario == 'exit3' else 0)
"""
CASES = {
    'good': set(), 'oldOrder': set(),
    'finalDenied': {'sensor'},
    'sensorAndArgs': {'sensor', 'actual_python_arguments', 'hook_arguments', 'hook_results'},
    'missingSidecar': {'installed_sidecar'},
    'exit3': {'orderly_shutdown'},
    'survivor': {'owned_before_backup'},
    'malformedFinal': {'observation_errors', 'rpc_protocol'},
    'decodedLeak': {'observation_errors', 'rpc_privacy', 'sensor'},
    'stderrLeak': {'stderr_privacy', 'sensor'},
    'startupFailure': {'observation_errors', 'sensor', 'sensor_monotonic'},
}
MUTANTS = {
    'conjunction': ('"passed": all(value == "PASS" for value in report["checks"].values()),',
                    '"passed": report["passed"],'),
    'provenance': ('guards["installed_sidecar"] = bool(sidecars)', 'guards["installed_sidecar"] = True'),
    'finalDenied': ('all(c["denied.txt"] == 0 for c in phase_counts.values())',
                    'all(phase_counts[phase]["denied.txt"] == 0 for phase in PHASES[:-1])'),
}


def worker(case, installed, mutant):
    """Fresh dedicated subreaper; never let OwnedProcesses adopt caller children."""
    if p.isolation.children(os.getpid()):
        raise RuntimeError('synthetic worker must start child-free')
    if mutant:
        old, new = MUTANTS[mutant]
        source = (ROOT / 'permissions.py').read_text()
        assert source.count(old) == 1, 'synthetic mutant target changed'
        # Mutate a private in-memory helper, never runtime/source/config files.
        exec(compile(source.replace(old, new), '<synthetic helper mutant>', 'exec'), p.__dict__)
    cwd = Path.cwd()
    output = io.StringIO()
    assertion = None
    samples = []
    counts = p.ReadSensor.counts

    def observe(sensor):
        value = counts(sensor)
        samples.append(value)
        if case == 'startupFailure' and len(samples) == 1:
            raise OSError('synthetic-private-input')
        return value

    with p.isolation.subreaper(), patch.object(p.ReadSensor, 'counts', observe):
        with contextlib.redirect_stdout(output):
            try:
                p.live_check(dict(os.environ), cwd, Path(installed), 'litellm/fixture')
            except AssertionError as error:
                assertion = str(error)
    text = output.getvalue()
    lines = [line for line in text.splitlines() if line.startswith('permissions live ')]
    # Validate privacy before releasing the complete safe report, before outcome assertions.
    assert 'IUA_PRIVATE_' not in text and 'synthetic-private-input' not in text, 'unsafe synthetic report'
    assert len(lines) == 1, 'synthetic report missing'
    report = json.loads(lines[0].split(': ', 1)[1])
    # Missing observations stay unknown, never zero. Preserve the product report
    # internally while printing every phase, including an unavailable startup.
    safe_report = {**report, 'sensorPhases': {phase: report['sensorPhases'].get(phase,
        {'allowed.txt': None, 'denied.txt': None}) for phase in p.PHASES}}
    print('synthetic guard proof ' + case + (('/' + mutant) if mutant else '') + ': ' +
          json.dumps(safe_report, sort_keys=True), flush=True)
    identity = json.loads((cwd / 'sidecar-identity.json').read_text())
    current = p.isolation.process_info(identity[0])
    absent = current is None or current[0] != identity[1]
    children_absent = not p.isolation.children(os.getpid())
    print('synthetic backup cleanup: ' + json.dumps({'ownedSidecarAbsent': absent,
          'ownedChildrenAbsent': children_absent, 'sensorSamples': len(samples)}, sort_keys=True), flush=True)
    required = CASES[case]
    failed = {name for name, value in report['checks'].items() if value == 'FAIL'}
    assert len(report['checks']) == 36, 'incomplete synthetic check report'
    assert len(samples) == 4, 'synthetic sensor sampling changed'
    assert report['passed'] == (not required), 'synthetic final verdict mismatch'
    assert assertion == ('permissions invariant failure' if required else None), 'synthetic final AND lost'
    assert required == failed, 'synthetic guard outcomes changed'
    assert report['survivorsBeforeBackup'] == (1 if case == 'survivor' else 0), 'synthetic pre-backup survivors'
    assert absent and children_absent, 'synthetic owned children survived backup'
    evidence = json.loads((cwd / 'host-evidence.json').read_text())
    assert evidence == {'allowedRead': True, 'deniedBlocked': True, 'eventsAfterRead': True}, 'synthetic host IO missing'
    phases = report['sensorPhases']
    assert set(phases) == set(p.PHASES) - ({'startup'} if case == 'startupFailure' else set()), 'synthetic phases missing'
    for phase, sample in zip(p.PHASES, samples):
        if phase in phases:
            assert phases[phase] == sample, 'synthetic cumulative counters changed'
    assert samples[0] == samples[1] == {'allowed.txt': 0, 'denied.txt': 0}, 'synthetic pre-prompt access'
    assert samples[2]['allowed.txt'] > 0, 'synthetic allowed baseline absent'
    assert all(a[path] <= b[path] for a, b in zip(samples, samples[1:])
               for path in ('allowed.txt', 'denied.txt')), 'synthetic counters reset'
    if case == 'finalDenied':
        assert samples[2]['denied.txt'] == 0 and samples[3]['denied.txt'] > 0, 'synthetic late read not exercised'
    assert all(h.get('path') in (None, 'allowed.txt', 'denied.txt') and
               type(h['argumentMatches']) is bool and type(h['idMatches']) is bool
               for h in report['hooks']), 'unsafe synthetic hook projection'


def fixture_env(root):
    # No inherited credentials/model/session/config hooks, even if parent selected a model.
    return {'PATH': os.environ.get('PATH', os.defpath), 'HOME': str(root), 'TMPDIR': str(root),
            'PYTHONDONTWRITEBYTECODE': '1', 'HINDSIGHT_DISABLED': 'true'}


class AssemblyControls(unittest.TestCase):
    def test_fixture_settles_after_all_tool_and_message_events(self):
        rpc, _policy, _phases = controls.DiagnosticControls().fixture()
        self.assertEqual(rpc.events[-1]['type'], 'agent_settled')
        self.assertFalse(any(e['type'] == 'agent_settled' for e in rpc.events[:-1]))

    def run_case(self, root, case, mutant=''):
        cwd = root / (case + (('-' + mutant) if mutant else ''))
        cwd.mkdir(mode=0o700)
        installed = root / 'installed'
        sidecar = installed / 'packages/rlm-kernel/python/sidecar.py'
        if not sidecar.exists():
            sidecar.parent.mkdir(parents=True)
            sidecar.write_text(SIDECAR)
        host = cwd / 'synthetic-host'
        host.write_text(HOST.replace('PYTHON', sys.executable, 1).replace('ROOT_LITERAL', repr(str(ROOT)), 1))
        host.chmod(0o700)
        (cwd / 'node-host.mjs').write_text(NODE.replace('MODULE_URI', json.dumps((ROOT / 'permissions-probe.mjs').as_uri())))
        env = {**fixture_env(root), 'PI_BIN': str(host), 'ASSEMBLY_CASE': case, 'ASSEMBLY_SIDECAR': str(sidecar)}
        expected_installed = root / 'not-installed' if case == 'missingSidecar' else installed
        result = p.isolation.run_owned_capture([sys.executable, str(Path(__file__).resolve()), '--worker',
            case, str(expected_installed), mutant], cwd=cwd, env=env, timeout=8)
        # Worker releases only safe diagnostics; never print uncontrolled traceback/stderr.
        safe = [line for line in result.stdout.splitlines() if line.startswith(
            ('synthetic guard proof ', 'synthetic backup cleanup: '))]
        print('\n'.join(safe), flush=True)
        self.assertNotIn('IUA_PRIVATE_', result.stdout, 'unsafe fixture stdout')
        self.assertNotIn('synthetic-private-input', result.stdout, 'unsafe fixture stdout')
        self.assertEqual(len(safe), 2, 'complete safe diagnostics missing')
        self.assertIn('"ownedSidecarAbsent": true', safe[1])
        self.assertIn('"ownedChildrenAbsent": true', safe[1])
        cleanups = [line for line in result.stderr.splitlines() if line.startswith('isolated-check cleanup: ')]
        self.assertEqual(len(cleanups), 2, 'inner/outer cleanup evidence missing')
        self.assertTrue(all(line.endswith(', 0 survivors') for line in cleanups), 'owned backup failed')
        return result

    def test_positive_and_negative_assembly(self):
        with tempfile.TemporaryDirectory(prefix='pi-c9b-assembly-') as directory:
            root = Path(directory)
            # Unrelated caller child is deliberately outside every dedicated supervisor.
            sentinel = subprocess.Popen([sys.executable, '-c', SIDECAR], env=fixture_env(root),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                self.assertEqual(sentinel.stdout.readline(), 'ready\n')
                for case in CASES:
                    with self.subTest(case=case):
                        self.assertEqual(self.run_case(root, case).returncode, 0, 'synthetic assembly rejected expected outcome')
                        self.assertIsNone(sentinel.poll(), 'unrelated sentinel signaled')
            finally:
                sentinel.terminate()
                sentinel.wait(timeout=2)
                sentinel.stdout.close()
        self.assertFalse(root.exists(), 'synthetic fixture root survived')

    def test_guard_mutants_fail_retained_test(self):
        with tempfile.TemporaryDirectory(prefix='pi-c9b-mutants-') as directory:
            root = Path(directory)
            for mutant, case in (('conjunction', 'missingSidecar'), ('provenance', 'missingSidecar'),
                                 ('finalDenied', 'finalDenied')):
                with self.subTest(mutant=mutant):
                    self.assertEqual(self.run_case(root, case, mutant).returncode, 1,
                                     'synthetic guard mutant escaped retained test')
        self.assertFalse(root.exists(), 'synthetic mutant root survived')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--worker']:
        try:
            worker(*sys.argv[2:])
        except Exception:
            print('synthetic worker assertion failed', file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        unittest.main(verbosity=2)
