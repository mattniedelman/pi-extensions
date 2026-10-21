"""Retained Linux stopped-Monty regression with a test-only backup observer.

Each observer is a fresh subreaper with exactly one Node command. An unrelated
live sentinel is owned by the outer harness, never part of an observer's tree.
The existing runner is reused only for test observation/backup, not production.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("runner", HERE / "isolated-check.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def observe(mode, root, node):
    assert not runner.children(os.getpid()), "observer must start fresh"
    with runner.subreaper():
        proc = subprocess.Popen([node, str(HERE / "stopped-worker-probe.mjs"), mode, root])
        owned = runner.OwnedProcesses(proc)
        try:
            deadline = time.monotonic() + 10
            while proc.poll() is None and time.monotonic() < deadline:
                owned.remaining()  # Discover positive child edges and reap only.
                time.sleep(.005)
            deadline = time.monotonic() + .3
            while owned.remaining() and time.monotonic() < deadline:
                time.sleep(.005)
            survivors = owned.remaining()
            print(f"OBSERVER BEFORE backup {mode}: " + json.dumps({
                "status": proc.poll(), "observed": sorted(owned.observed),
                "survivors": survivors}), flush=True)
            status = 0 if proc.poll() == 0 and not survivors else 1
        finally:
            owned.cleanup()
        return status


def main(node):
    modes = ["startup-timeout", "startup-abort", "startup-close", "request-timeout"]
    # No tool/caller children belong to the product's subprocess scope.
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    identity = runner.process_info(sentinel.pid)[0]
    children = []
    try:
        with tempfile.TemporaryDirectory(prefix="pi-rlm-stopped-") as directory:
            root = Path(directory)
            for mode in modes:
                path = root / mode
                path.mkdir()
                children.append(subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                                   "--observe", mode, str(path), node]))
            statuses = []
            for child in children:
                statuses.append(child.wait(timeout=12))
                assert sentinel.poll() is None
                assert runner.process_info(sentinel.pid)[0] == identity
            print(f"UNRELATED sentinel alive: {sentinel.pid}/{identity}", flush=True)
            assert statuses == [0] * len(modes), f"stopped-worker case statuses: {statuses}"
        assert not root.exists(), "private fixture root survived removal"
        print("rlm-kernel stopped Monty checks passed: 4/4; absence BEFORE backup; unrelated sentinel alive", flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
        sentinel.terminate()
        sentinel.wait(timeout=1)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--observe"]:
        sys.exit(observe(*sys.argv[2:]))
    main(sys.argv[1])
