#!/usr/bin/env python3
"""Run Linux baseline checks with private pi config and bounded owned cleanup.

Usage: isolated-check.py --self-test
       isolated-check.py --timeout 120 -- npm test

The command retains the caller's cwd; pi (via PATH and PI_BIN) runs in a private
no-context cwd with only explicit extensions. uv keeps its normal dependency
cache, but uses private venvs. This is test isolation, not a security sandbox.
Exit: command status, 124 deadline, 128+signal interruption, 125 cleanup/setup
failure, 127 missing command. TERM/INT/HUP are handled; allow at least 3s beyond
the internal deadline in an outer TERM guard. SIGKILL cannot be handled by any
in-process runner. Linux /proc, child subreaping and pidfd signals are required.
"""
import argparse
from contextlib import contextmanager
import ctypes
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time


@contextmanager
def isolated_environment(source=None):
    source = dict(os.environ if source is None else source)
    pi = shutil.which(source.get("PI_BIN", "pi"), path=source.get("PATH"))
    if pi is None:
        raise RuntimeError("pi executable not found (set PI_BIN explicitly)")
    pi = str(Path(pi).resolve())
    # An allowlist avoids forwarding model/cloud credentials, session identity,
    # extension hooks (NODE_OPTIONS/PYTHONPATH), or personal loadout overrides.
    env = {key: source[key] for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM") if key in source}
    cache = source.get("UV_CACHE_DIR", str(Path(source.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "uv"))
    cache = str(Path(cache).resolve())
    with tempfile.TemporaryDirectory(prefix="pi-isolated-check-") as directory:
        root = Path(directory)
        for name in ("agent", "state", "sessions", "cwd", "home", "tmp", "bin", "config", "cache"):
            (root / name).mkdir(mode=0o700)
        env.update({
            "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
            "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
            "PI_CODING_AGENT_DIR": str(root / "agent"), "PI_RLM_STATE_DIR": str(root / "state"),
            "PI_CODING_AGENT_SESSION_DIR": str(root / "sessions"),
            "HINDSIGHT_DISABLED": "true", "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0", "UV_CACHE_DIR": cache,
            "UV_PROJECT_ENVIRONMENT": str(root / "venv"), "PYTHONDONTWRITEBYTECODE": "1",
        })
        wrapper = root / "bin" / "pi"
        flags = ["--offline", "--no-extensions", "--no-mcp", "--no-skills", "--no-prompt-templates",
                 "--no-themes", "--no-context-files"]
        wrapper.write_text(f"#!{sys.executable}\nimport os, sys\n" +
                           f"os.chdir({str(root / 'cwd')!r})\n" +
                           f"os.execv({pi!r}, [{pi!r}, *{flags!r}, *sys.argv[1:]])\n")
        wrapper.chmod(0o700)
        env["PATH"] = str(root / "bin") + os.pathsep + source.get("PATH", os.defpath)
        env["PI_BIN"] = str(wrapper)
        yield env


def process_info(pid):
    """Identity and parent from a *known* PID, including zombies until reaped."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19], int(fields[1])  # starttime (field 22), ppid
    except (FileNotFoundError, ProcessLookupError):
        return None


def children(pid):
    """Walk only the known process's threads, not the global /proc PID table."""
    try:
        tasks = list(Path(f"/proc/{pid}/task").iterdir())
    except (FileNotFoundError, ProcessLookupError):
        return set()
    result = set()
    for task in tasks:
        try:
            result.update(int(value) for value in (task / "children").read_text().split())
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


@contextmanager
def subreaper():
    # Adoption closes the fast-parent-exit and separately-grouped-child holes
    # without searching unrelated processes. Fail closed on unsupported hosts.
    if not sys.platform.startswith("linux"):
        raise RuntimeError("isolated-check requires Linux /proc and child subreaping")
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0 or libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not enable child subreaping")
    try:
        yield
    finally:
        if libc.prctl(36, previous.value, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "could not restore child subreaping")


class OwnedProcesses:
    """Track the sole command tree of a fresh, dedicated supervisor.

    The supervisor starts with no children and launches exactly one command.
    Thus every child edge in this scope has positive command ancestry, including
    fast-exiting parents' adoptees. This class must not run in the caller: caller
    adoptees can belong to unrelated pre-existing or dynamically forked trees.
    """
    def __init__(self, proc):
        self.proc = proc
        self.seen = {}  # pid -> current starttime
        self.observed = set()  # (pid, starttime), including owned PID reuse

    def discover(self):
        # Fresh scope roots carry provenance; old observations only remain
        # authorized while the identity matches. A recycled PID is not a root.
        pending = [(pid, info[0]) for pid in children(os.getpid())
                   if (info := process_info(pid)) and info[1] == os.getpid()]
        pending.extend(self.seen.items())
        visited = set()
        while pending:
            pid, expected = pending.pop()
            info = process_info(pid)
            if info is None or expected != info[0]:
                continue
            identity = (pid, info[0])
            if identity in visited:
                continue
            visited.add(identity)
            self.seen[pid] = info[0]
            self.observed.add(identity)
            descendants = children(pid)
            after = process_info(pid)
            if after is None or after[0] != info[0]:
                continue  # do not walk a recycled parent's unrelated subtree
            pending.extend((child, child_info[0]) for child in descendants
                           if (child_info := process_info(child)) and child_info[1] == pid)

    def remaining(self):
        self.discover()
        self.proc.poll()  # wait/reap the Popen child first
        for pid, identity in self.seen.items():
            info = process_info(pid)
            if pid != self.proc.pid and info and info[0] == identity and info[1] == os.getpid():
                try:
                    os.waitpid(pid, os.WNOHANG)  # only adopted, owned children
                except ChildProcessError:
                    pass
        return [pid for pid, identity in self.seen.items()
                if (info := process_info(pid)) and info[0] == identity]

    def send(self, sig):
        # Signal every owned member of the private initial group and every
        # escaped descendant individually. No group-wide authorization can
        # accidentally include an unrelated member or a recycled group ID.
        for pid in self.remaining():
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            try:
                info = process_info(pid)
                if info and info[0] == self.seen[pid]:
                    # The handle pins the process across the check/signal gap.
                    signal.pidfd_send_signal(fd, sig)
            except ProcessLookupError:
                pass
            finally:
                os.close(fd)

    def cleanup(self):
        # Re-discover on each pass, including newly adopted descendants after
        # parents exit. Repeated signals also cover children forked during TERM.
        for sig, duration in ((signal.SIGTERM, .4), (signal.SIGKILL, 1.0)):
            deadline = time.monotonic() + duration
            while self.remaining():
                self.send(sig)
                if time.monotonic() >= deadline:
                    break
                time.sleep(.01)
        survivors = self.remaining()
        if survivors:
            raise RuntimeError(f"owned descendants survived cleanup: {survivors}")
        self.proc.wait(timeout=.2)
        print(f"isolated-check cleanup: {len(self.observed)} owned PIDs observed, 0 survivors", file=sys.stderr, flush=True)


def supervise(command, *, timeout, deadline):
    """Private entry point in a new interpreter, never in the caller process."""
    if children(os.getpid()):
        raise RuntimeError("supervisor must start without pre-existing children")
    interrupted = 0

    def on_signal(number, _frame):
        nonlocal interrupted
        # Do not raise in Popen's spawn/assignment window or during cleanup.
        interrupted = number

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    for sig in previous:
        signal.signal(sig, on_signal)
    try:
        with subreaper():
            # Fail before launching anything if safe PID-handle signaling is
            # unavailable, rather than discovering that during teardown.
            if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
                raise RuntimeError("isolated-check requires Linux pidfd signal support")
            fd = os.pidfd_open(os.getpid())
            try:
                signal.pidfd_send_signal(fd, 0)
            finally:
                os.close(fd)
            try:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)
            except FileNotFoundError as error:
                print(f"isolated-check: {error}", file=sys.stderr)
                return 127
            owned = OwnedProcesses(proc)
            try:
                while True:
                    owned.discover()
                    if interrupted:
                        status = 128 + interrupted
                        break
                    status = proc.poll()
                    if status is not None:
                        status = status if status >= 0 else 128 - status
                        break
                    if time.monotonic() >= deadline:
                        print(f"isolated-check: deadline exceeded ({timeout:g}s)", file=sys.stderr, flush=True)
                        status = 124
                        break
                    time.sleep(.01)
            finally:
                owned.cleanup()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    # A handled signal during cleanup must still cancel a successful command.
    # Cleanup/setup exceptions reach the CLI as 125 before this selection.
    return 128 + interrupted if interrupted and status != 125 else status


def run_owned(command, *, cwd, env, timeout):
    """Keep unrelated caller subtrees outside a dedicated command supervisor."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    interrupted = 0

    def on_signal(number, _frame):
        nonlocal interrupted
        interrupted = number

    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    for sig in previous:
        signal.signal(sig, on_signal)
    proc = None
    deadline = time.monotonic() + timeout
    try:
        # Anonymous input avoids exposing commands in the supervisor argv or
        # blocking on a full pipe before signal/deadline handling is active.
        with tempfile.TemporaryFile(mode="w+") as request:
            json.dump({"command": list(map(os.fspath, command)), "timeout": timeout,
                       "deadline": deadline}, request)
            request.seek(0)
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--_supervise"],
                                    cwd=cwd, env=env, stdin=request, start_new_session=True)
        forwarded = 0
        while proc.poll() is None:
            if interrupted and interrupted != forwarded:
                proc.send_signal(interrupted)
                forwarded = interrupted
            # The supervisor has its own deadline and bounded cleanup. Leave
            # enough time for both before treating a wedged supervisor as failure.
            if time.monotonic() >= deadline + 3:
                raise RuntimeError("supervisor exceeded deadline and cleanup allowance")
            time.sleep(.01)
        status = proc.returncode if proc.returncode >= 0 else 128 - proc.returncode
    finally:
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # Like external SIGKILL, killing a wedged supervisor cannot
                    # promise cleanup of unobserved escaped descendants.
                    proc.kill()
                    proc.wait(timeout=1)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    # The caller can be interrupted after the supervisor has already exited
    # (too late to forward). Its own cancellation still wins, except for 125.
    return 128 + interrupted if interrupted and status != 125 else status


def main():
    if sys.argv[1:] == ["--_supervise"]:
        request = json.load(sys.stdin)
        return supervise(**request)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.self_test:
        if args.command:
            parser.error("--self-test does not take a command")
        # Own the whole fixture process as well: an outer guard firing between
        # individual checks must not orphan the self-test's sentinel/fixtures.
        return run_owned([sys.executable, str(Path(__file__).with_name("isolated_check_tests.py").resolve()),
                          str(Path(__file__).resolve())], cwd=Path.cwd(), env=dict(os.environ), timeout=15)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    if not args.command or args.command[0] != "--" or len(args.command) == 1:
        parser.error("expected -- COMMAND")
    cwd = Path.cwd()  # explicit repo cwd for npm; never chdir the caller
    with isolated_environment() as env:
        return run_owned(args.command[1:], cwd=cwd, env=env, timeout=args.timeout)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError) as error:
        print(f"isolated-check: {error}", file=sys.stderr)
        sys.exit(125)
