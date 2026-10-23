"""Real Monty worker stopped after checkout; test-only sidecar entry point."""
import json
import os
from pathlib import Path
import signal
import sys
import time

import sidecar


def info(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return {"pid": pid, "start": fields[19], "parent": int(fields[1]),
            "group": int(fields[2]), "session": int(fields[3]), "state": fields[0]}


mode, record, state = sys.argv[1:]
if mode == "restart":
    previous = json.loads(Path(record).read_text())
    for identity in [previous["uv"], previous["python"], *previous["workers"]]:
        try:
            current = info(identity["pid"])
        except FileNotFoundError:
            continue
        assert current["start"] != identity["start"], "restart began before old tree retirement"
    print("RESTART old UV/Python/Monty absent before new Kernel", file=sys.stderr, flush=True)
    sys.argv = [__file__, "--state", state]
    sidecar.main()
    sys.exit(0)

original = sidecar.Kernel


class StoppedKernel(original):
    def __init__(self, path):
        super().__init__(path)
        workers = set()
        for task in Path(f"/proc/{os.getpid()}/task").iterdir():
            workers.update(int(pid) for pid in (task / "children").read_text().split())
        assert workers, "expected real Monty child"
        parent = info(os.getpid())
        identities = [info(pid) for pid in workers]
        assert all(worker["parent"] == parent["pid"] for worker in identities)
        for worker in identities:
            os.kill(worker["pid"], signal.SIGSTOP)
        deadline = time.monotonic() + 1
        while any(info(pid)["state"] != "T" for pid in workers):
            assert time.monotonic() < deadline, "worker did not stop"
            time.sleep(.005)
        temp = Path(record).with_suffix(".tmp")
        temp.write_text(json.dumps({"python": parent, "workers": [info(pid) for pid in workers]}))
        temp.replace(record)
        if mode != "request-timeout":
            while True:
                time.sleep(.05)


sidecar.Kernel = StoppedKernel
sys.argv = [__file__, "--state", state]
sidecar.main()
