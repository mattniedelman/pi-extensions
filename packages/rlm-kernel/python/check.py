import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def run(requests, state):
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "sidecar.py"), "--state", str(state)],
        env={**__import__("os").environ, "PI_RLM_MAX_MEMORY": "8000000"},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdin and proc.stdout
    assert json.loads(proc.stdout.readline())["ready"] is True
    replies = []
    for request in requests:
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        replies.append(json.loads(proc.stdout.readline()))
    proc.stdin.write(json.dumps({"id": 999, "operation": "shutdown"}) + "\n")
    proc.stdin.flush()
    assert json.loads(proc.stdout.readline())["ok"]
    assert proc.wait(timeout=5) == 0
    return replies


with tempfile.TemporaryDirectory() as directory:
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
    corrupt.write_bytes(b"not a monty dump")
    recovered = run([{"id": 11, "operation": "execute", "code": "1 + 1"}], corrupt)
    assert recovered[0]["result"]["result"] == 2
    assert corrupt.with_suffix(".corrupt").exists()

    reset = run([
        {"id": 12, "operation": "execute", "code": "z = 3"},
        {"id": 13, "operation": "checkpoint"},
        {"id": 14, "operation": "reset"},
        {"id": 15, "operation": "execute", "code": "z"},
    ], base / "reset.bin")
    assert reset[1]["result"]["checkpointed"] is True
    assert reset[2]["result"]["reset"] is True
    assert reset[3]["ok"] is False and "NameError" in reset[3]["error"]

    long_error = run([{"id": 16, "operation": "execute", "code": "raise Exception('a' * 100000)"}], base / "error.bin")
    assert long_error[0]["ok"] is False
    assert len(long_error[0]["error"].encode()) <= 8192

subprocess.run([sys.executable, str(ROOT / "recovery_check.py")], check=True, timeout=20)
print("rlm-kernel sidecar checks passed")
