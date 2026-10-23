import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from recovery_check import run_checks, runner

ROOT = Path(__file__).parent


def run(requests, state):
    messages = [*requests, {"id": 999, "operation": "shutdown"}]
    proc = runner.run_owned_capture(
        [sys.executable, str(ROOT / "sidecar.py"), "--state", str(state)],
        input="".join(json.dumps(request) + "\n" for request in messages),
        cwd=ROOT, timeout=5,
        env={**os.environ, "PI_RLM_MAX_MEMORY": "8000000"},
    )
    assert proc.returncode == 0, proc.stderr
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert replies[0] == {"ready": True}, replies
    assert len(replies) == len(messages) + 1, replies
    assert [reply["id"] for reply in replies[1:]] == [request["id"] for request in messages]
    assert replies[-1]["ok"], replies[-1]
    return replies[1:-1]


def private(path, mode):
    assert stat.S_IMODE(path.stat().st_mode) == mode, path


with tempfile.TemporaryDirectory(prefix="pi-rlm-sidecar-") as directory:
    base = Path(directory)
    state_a = base / "a.bin"
    first = run([
        {"id": 1, "operation": "execute", "code": "x = 40\nx + 2"},
        {"id": 2, "operation": "execute", "code": "x + 3"},
        {"id": 3, "operation": "execute", "code": "print('a' * 9000)"},
        {"id": 4, "operation": "execute", "code": "float('inf')"},
        {"id": 5, "operation": "execute", "code": "print('\U0001f600' * 9000)"},
    ], state_a)
    assert first[0]["result"]["result"] == 42
    assert first[1]["result"]["result"] == 43
    assert first[2]["result"]["truncated"] is True
    assert len(first[2]["result"]["output"].encode()) <= 4096
    assert first[3]["result"]["result"] == "inf"
    assert first[4]["result"]["truncated"] is True
    assert len(first[4]["result"]["output"].encode()) <= 4096
    private(state_a.parent, 0o700)
    for path in (state_a, state_a.with_suffix(".names.json")):
        private(path, 0o600)
    assert not state_a.with_suffix(".tmp").exists()
    assert not state_a.with_suffix(".names.tmp").exists()

    restored = run([
        {"id": 4, "operation": "execute", "code": "x"},
        {"id": 5, "operation": "execute", "code": "while True:\n    pass"},
        {"id": 6, "operation": "execute", "code": "x"},
        {"id": 7, "operation": "execute", "code": "blob = 'a' * 12000000"},
        {"id": 8, "operation": "execute", "code": "x"},
        {"id": 9, "operation": "variables"},
    ], state_a)
    assert restored[0]["result"]["result"] == 40
    assert restored[1]["ok"] is False and "TimeoutError" in restored[1]["error"]
    assert restored[2]["result"]["result"] == 40
    assert restored[3]["ok"] is False and "MemoryError" in restored[3]["error"]
    assert restored[4]["result"]["result"] == 40
    assert restored[5]["result"]["names"] == ["x"]

    isolated = run([{"id": 10, "operation": "execute", "code": "x"}], base / "b.bin")
    assert isolated[0]["ok"] is False and "NameError" in isolated[0]["error"]

    corrupt = base / "corrupt.bin"
    damaged = b"not a monty dump"
    corrupt.write_bytes(damaged)
    corrupt.chmod(0o600)
    corrupt.with_suffix(".names.json").write_text('["stale_name"]')
    recovered = run([
        {"id": 11, "operation": "variables"},
        {"id": 12, "operation": "execute", "code": "stale_name"},
        {"id": 13, "operation": "execute", "code": "1 + 1"},
    ], corrupt)
    assert recovered[0]["result"]["names"] == []
    assert recovered[1]["ok"] is False and "NameError" in recovered[1]["error"]
    assert recovered[2]["result"]["result"] == 2
    quarantine = corrupt.with_suffix(".corrupt")
    assert quarantine.read_bytes() == damaged
    for path in (corrupt, corrupt.with_suffix(".names.json"), quarantine):
        private(path, 0o600)

    reset_state = base / "reset" / "kernel.bin"
    seeded = run([
        {"id": 12, "operation": "execute", "code": "z = 3"},
        {"id": 13, "operation": "checkpoint"},
    ], reset_state)
    assert seeded[1]["result"]["checkpointed"] is True
    retained = {
        reset_state.with_suffix(".corrupt"): b"private quarantine",
        reset_state.with_suffix(".tmp"): b"private raw leftover",
        reset_state.with_suffix(".names.tmp"): b"private names leftover",
        base / ".venv" / "marker": b"shared dependencies",
    }
    for path, content in retained.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(0o600)
    reset = run([
        {"id": 14, "operation": "reset"},
        {"id": 15, "operation": "variables"},
        {"id": 16, "operation": "execute", "code": "z"},
        {"id": 17, "operation": "reset"},
    ], reset_state)
    assert reset[0]["result"]["reset"] is True
    assert reset[1]["result"]["names"] == []
    assert reset[2]["ok"] is False and "NameError" in reset[2]["error"]
    assert reset[3]["result"]["reset"] is True
    assert not reset_state.exists()
    assert not reset_state.with_suffix(".names.json").exists()
    for path, content in retained.items():
        assert path.read_bytes() == content
        private(path, 0o600)
    private(reset_state.parent, 0o700)
    reset_restored = run([{"id": 18, "operation": "execute", "code": "z"}], reset_state)
    assert reset_restored[0]["ok"] is False and "NameError" in reset_restored[0]["error"]
    assert not reset_state.exists()
    print("rlm actual-file privacy/quarantine/reset retention checks passed: 3/3")

    long_error = run([{"id": 16, "operation": "execute", "code": "raise Exception('a' * 100000)"}], base / "error.bin")
    assert long_error[0]["ok"] is False
    assert len(long_error[0]["error"].encode()) <= 8192

assert not base.exists(), "private sidecar fixture root survived cleanup"
run_checks()
print("rlm sidecar checks passed")
