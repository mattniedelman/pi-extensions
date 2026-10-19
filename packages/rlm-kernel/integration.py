#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parent


def children(pid: int) -> list[int]:
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        direct = [int(value) for value in path.read_text().split()]
    except FileNotFoundError:
        return []
    return direct + [descendant for child in direct for descendant in children(child)]


def request(proc, request_id, request_type, **values):
    proc.stdin.write(json.dumps({"id": request_id, "type": request_type, **values}) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(proc.stderr.read())
        message = json.loads(line)
        if message.get("type") == "response" and message.get("id") == request_id:
            return message
    raise TimeoutError(request_type)


with tempfile.TemporaryDirectory(prefix="pi-rlm-integration-") as directory:
    directory = Path(directory)
    output = directory / "probe.json"
    probe = directory / "probe.ts"
    probe.write_text("import { writeFileSync } from 'node:fs';\n" +
        "export default function (pi) {\n" +
        "  pi.registerCommand('probe-rlm', { description: 'Probe the RLM extension', handler: async () => {\n" +
        "    const tool = pi.getAllTools().find((item) => item.name === 'python');\n" +
        f"    writeFileSync({json.dumps(str(output))}, JSON.stringify(tool));\n" +
        "  }});\n}\n")
    env = {**os.environ, "PI_RLM_STATE_DIR": str(directory / "state")}
    proc = subprocess.Popen([
        os.environ.get("PI_BIN", "pi"), "--mode", "rpc", "--no-session", "--no-mcp", "--no-skills",
        "--extension", str(ROOT / "index.ts"), "--extension", str(probe),
    ], cwd=directory, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdin and proc.stdout and proc.stderr
    try:
        assert request(proc, "1", "get_state")["success"] is True
        assert request(proc, "2", "prompt", message="/probe-rlm")["success"] is True
        deadline = time.monotonic() + 5
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        tool = json.loads(output.read_text())
        assert tool["name"] == "python"
        assert tool["exposure"] == "model-only"
        assert "pi.read" in tool["description"]
        descendants = children(proc.pid)
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 143
    time.sleep(0.1)
    assert all(not Path(f"/proc/{pid}").exists() for pid in descendants)

print("rlm-kernel extension integration check passed")
