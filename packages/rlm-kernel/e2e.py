#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description="Opt-in real-pi RLM checks")
parser.add_argument("--case", choices=("resume", "permissions"), default="resume")
parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--model", help=argparse.SUPPRESS)
args = parser.parse_args()
pi = os.environ.get("PI_BIN", "pi")
model = args.model if args._worker else os.environ.get("PI_RLM_TEST_MODEL")
if not model:
    print(f"SKIP: {args.case}: set PI_RLM_TEST_MODEL=provider/model for live pi e2e")
    raise SystemExit(0)
if args.case == "permissions":
    import permissions
    try:
        if args._worker:
            permissions.worker(model)
            status = 0
        else:
            status = permissions.run(model)
    except BaseException as error:
        # Never print raw provider errors, state/model dictionaries or secrets.
        safe = str(error) if isinstance(error, AssertionError) and len(error.args) == 1 and isinstance(error.args[0], str) else type(error).__name__
        print(f"permissions FAIL: {safe}", file=__import__("sys").stderr, flush=True)
        status = 1
    raise SystemExit(status)

provider, model_id = model.split("/", 1)
with tempfile.TemporaryDirectory(prefix="pi-rlm-e2e-") as directory:
    directory = Path(directory)
    sessions = directory / "sessions"
    sessions.mkdir()
    (directory / "large.txt").write_text("ignore\nneedle one\nskip\nneedle two\n")
    base = [
        pi, "--session-dir", str(sessions), "--session-id", "rlm-e2e",
        "--extension", str(root / "index.ts"), "--provider", provider,
        "--model", model_id, "--thinking", "off", "-p",
    ]
    env = {**os.environ, "PI_RLM_STATE_DIR": str(directory / "state")}
    first = subprocess.run(base + ["Use the python tool exactly once with code x = 40 followed by x + 2. Then report only its result."], cwd=directory, env=env, text=True, capture_output=True, check=True)
    assert first.stdout.strip() == "42", first.stdout + first.stderr
    second = subprocess.run(base + ["Use the python tool exactly once with code x + 3. Then report only its result."], cwd=directory, env=env, text=True, capture_output=True, check=True)
    assert second.stdout.strip() == "43", second.stdout + second.stderr
    third = subprocess.run(base + ["Use the python tool exactly once with this exact code: doc = pi.read(path='large.txt'); matches = [line for line in doc['content'].splitlines() if line.strip().startswith('needle')]; len(matches). Report only the number."], cwd=directory, env=env, text=True, capture_output=True, check=True)
    assert third.stdout.strip() == "2", third.stdout + third.stderr
    fourth = subprocess.run(base + ["Use the python tool exactly once with code matches[1]. Then report only its result text."], cwd=directory, env=env, text=True, capture_output=True, check=True)
    assert fourth.stdout.strip() == "needle two", fourth.stdout + fourth.stderr
print("rlm-kernel live pi resume and nested read check passed")
