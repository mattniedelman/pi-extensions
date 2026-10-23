#!/usr/bin/env python3
"""Offline controls for the installed real-pi lifecycle adapter; no inference."""
import copy
import json
import subprocess
import sys
from pathlib import Path


def oracle_controls():
    import lifecycle as l
    start = {"type": "tool_execution_start", "toolName": "python", "toolCallId": "parent", "args": {"code": l.RETAINED}}
    end = {"type": "tool_execution_end", "toolName": "python", "toolCallId": "parent", "isError": False,
           "result": {"details": {"result": l.VALUE42, "output": "", "truncated": False}}}
    events = [start, end, {"type": "agent_settled", "aborted": False}]
    l.assert_cell(events, l.RETAINED, l.VALUE42)
    for fault in ("wrong-value", "false-zero", "wrong-id", "wrong-code", "unsettled"):
        bad = copy.deepcopy(events)
        if fault == "wrong-value": bad[1]["result"]["details"]["result"]["x"] = 41
        elif fault == "false-zero":
            bad[1]["result"]["details"]["result"]["x"] = False
        elif fault == "wrong-id": bad[1]["toolCallId"] = "other"
        elif fault == "wrong-code": bad[0]["args"]["code"] = "x = 42"
        else: bad[-1]["aborted"] = True
        try:
            expected = {**l.VALUE42, "x": 0} if fault == "false-zero" else l.VALUE42
            l.assert_cell(bad, l.RETAINED, expected)
        except AssertionError:
            pass
        else:
            raise AssertionError("negative lifecycle oracle accepted")
    # A synthetic transcript cannot satisfy paid-provider proof.
    try:
        l.assert_usage([{"role": "assistant", "provider": "litellm", "model": "fixture",
                         "stopReason": "stop", "usage": {"totalTokens": 0}}], "litellm/fixture", replay=False)
    except AssertionError:
        pass
    else:
        raise AssertionError("zero usage accepted as live proof")
    print("lifecycle oracle controls: 7/7 PASS (positive + six negatives)", flush=True)


def skip_control():
    import os
    import tempfile
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="pi-lifecycle-skip-") as directory:
        env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": directory,
               "PI_CODING_AGENT_DIR": directory + "/unavailable-config", "PI_BIN": directory + "/no-runtime",
               "PI_RLM_TEST_MODEL": "", "HINDSIGHT_DISABLED": "true", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(root / "e2e.py")], env=env,
                                capture_output=True, text=True, timeout=3)
        assert result.returncode == 0 and result.stdout.startswith("SKIP: lifecycle:") and not result.stderr
        assert not list(Path(directory).iterdir()), "SKIP performed setup"
    print("lifecycle early SKIP control: PASS; unavailable cfg/runtime untouched", flush=True)


if __name__ == "__main__":
    oracle_controls()
    skip_control()
    import lifecycle as l
    # Same adapter, actual install/discovery, RPC, Python callback and session APIs.
    raise SystemExit(l.run(l.REPLAY_SELECTOR, replay=True))
