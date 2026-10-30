#!/usr/bin/env python3
"""Validate one GNU-time completion-suite metric record."""
import argparse
import math
import re
from pathlib import Path

PATTERN = re.compile(r"^JUPYTER_SUITE elapsed=([^ ]+) max_rss_kb=([0-9]+) exit=([0-9]+)$")


def validate(text: str, max_rss_kib: int) -> dict[str, float | int]:
    lines = [line for line in text.splitlines() if line.startswith("JUPYTER_SUITE ")]
    if len(lines) != 1:
        raise ValueError("expected exactly one JUPYTER_SUITE metric")
    match = PATTERN.fullmatch(lines[0])
    if match is None:
        raise ValueError("malformed JUPYTER_SUITE metric")
    elapsed = float(match.group(1))
    rss = int(match.group(2))
    status = int(match.group(3))
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("elapsed must be finite and nonnegative")
    if status != 0:
        raise ValueError(f"suite exit was {status}")
    if rss > max_rss_kib:
        raise ValueError(f"max_rss_kb {rss} exceeds {max_rss_kib}")
    return {"elapsed": elapsed, "max_rss_kb": rss, "exit": status}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--max-rss-kib", type=int, required=True)
    args = parser.parse_args()
    result = validate(args.metrics.read_text(), args.max_rss_kib)
    print(f"completion budget PASS: max_rss_kb={result['max_rss_kb']} <= {args.max_rss_kib}; elapsed={result['elapsed']}; per-process high-water RSS")


if __name__ == "__main__":
    main()
