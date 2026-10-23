#!/usr/bin/env python3
"""Explicitly opted-in installed lifecycle smoke; permissions is optional, strict."""
import argparse
import os
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--case", choices=("lifecycle", "resume", "permissions"), default="lifecycle")
parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--_replay", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--model", help=argparse.SUPPRESS)
args = parser.parse_args()
model = args.model if args._worker else os.environ.get("PI_RLM_TEST_MODEL")
# Before any helper import, private config copy, installation or runtime bootstrap.
if not model:
    print(f"SKIP: {args.case}: set PI_RLM_TEST_MODEL=provider/model for live pi e2e")
    raise SystemExit(0)

import permissions
try:
    if args._replay and not args._worker:
        raise RuntimeError("replay requires dedicated offline worker")
    if args.case == "permissions":
        if args._worker:
            permissions.worker(model)
            status = 0
        else:
            status = permissions.run(model)
    else:
        import lifecycle
        if args._worker:
            lifecycle.worker(model, replay=args._replay)
            status = 0
        else:
            status = lifecycle.run(model)
except BaseException as error:
    # Never print raw provider errors, state/model dictionaries or secrets.
    fixed = {
        "private LiteLLM auth setup failed": "credential-unavailable",
        "private LiteLLM auth cleanup failed": "private-auth-cleanup-failed",
        "source auth changed during private check": "source-auth-changed",
    }
    safe = fixed.get(str(error))
    if safe is None and isinstance(error, FileNotFoundError):
        safe = "selected-config-unavailable"
    if safe is None and str(error).startswith("lifecycle configuration unavailable: "):
        categories = str(error).split(": ", 1)[1].split(",")
        if all(value in ("model-not-found", "credential-unavailable", "extension-load-failure") for value in categories):
            safe = "configuration-unavailable:" + ",".join(categories)
    if safe is None:
        safe = "invariant failure" if isinstance(error, AssertionError) else permissions.safe_error_type(error)
    print(f"{args.case} FAIL: {safe}", file=sys.stderr, flush=True)
    status = 1
raise SystemExit(status)
