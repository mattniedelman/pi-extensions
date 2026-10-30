import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent


def run(requests, state):
    messages = [*requests, {"id": 999, "operation": "shutdown"}]
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "sidecar.py"), "--state", str(state)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
        env=dict(os.environ),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(
            "".join(json.dumps(request) + "\n" for request in messages),
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    assert proc.returncode == 0, stderr
    replies = [json.loads(line) for line in stdout.splitlines()]
    ready = replies[0]
    assert ready.get("ready") is True and ready.get("generation"), replies
    assert len(replies) == len(messages) + 1, replies
    assert [reply["id"] for reply in replies[1:]] == [request["id"] for request in messages]
    assert replies[-1] == {"id": 999, "ok": True, "result": {"shutdown": True}}, replies[-1]
    return replies[1:-1]


with tempfile.TemporaryDirectory(prefix="pi-jupyter-check-") as directory:
    state = Path(directory) / "legacy.bin"
    legacy_paths = (
        state,
        state.with_suffix(".names.json"),
        state.with_suffix(".tmp"),
        state.with_suffix(".names.tmp"),
        state.with_suffix(".corrupt"),
    )
    replies = run(
        [
            {
                "id": 1,
                "operation": "execute",
                "code": (
                    "import os\n"
                    "class Counter:\n"
                    "    def __init__(self): self.values = [40]\n"
                    "counter = Counter()\n"
                    "kernel_pid = os.getpid()\n"
                    "(counter.values[0] + 2, kernel_pid)"
                ),
            },
            {"id": 2, "operation": "execute", "code": "counter.values.append(3)\nraise RuntimeError('x' * 20000)"},
            {"id": 3, "operation": "execute", "code": "counter.values"},
            {
                "id": 4,
                "operation": "execute",
                "code": "import sys\nprint('o' * 10000)\nprint('e' * 10000, file=sys.stderr)\n'r' * 10000",
            },
        ],
        state,
    )

    assert replies[0]["ok"], replies[0]
    value, kernel_pid = replies[0]["result"]["result"]
    assert value == 42, replies[0]
    assert not replies[1]["ok"] and "RuntimeError" in replies[1]["error"], replies[1]
    assert len(replies[1]["error"].encode()) <= 8192, replies[1]
    assert replies[2]["result"]["result"] == [40, 3], replies[2]
    assert replies[3]["result"]["truncated"] is True, replies[3]
    for field in ("output", "stderr", "result"):
        value = replies[3]["result"][field]
        encoded = value.encode() if isinstance(value, str) else json.dumps(value).encode()
        assert len(encoded) <= 4096, (field, len(encoded))
    assert not any(path.exists() for path in legacy_paths), legacy_paths

    kernel_path = Path(f"/proc/{kernel_pid}")
    deadline = time.monotonic() + 5
    while kernel_path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not kernel_path.exists(), f"kernel PID {kernel_pid} survived clean shutdown"

assert not Path(directory).exists(), "private direct-Jupyter fixture root survived cleanup"
print("rlm direct-Jupyter execute/shutdown checks passed: 5/5")
