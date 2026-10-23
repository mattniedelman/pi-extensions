#!/usr/bin/env python3
"""Installed opt-in lifecycle smoke; local transport controls are NOT live proof."""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

import permissions as p

ROOT = Path(__file__).resolve().parent
REPLAY_SELECTOR = "lifecycle-local-replay/fixed-python"
DOC = "ignore\nneedle one\nskip\nneedle two\n"
SEED = "x = 40\ndoc = pi.read(path='large.txt')\nmatches = [line for line in doc['content'].splitlines() if line.strip().startswith('needle')]\n{'x': x, 'doc': doc['content'], 'matches': matches, 'read_ok': not doc['is_error']}"
RETAINED = "{'x': x, 'doc': doc['content'], 'matches': matches, 'read_ok': not doc['is_error']}"
ADVANCE = "x = x + 2\n" + RETAINED
ISOLATED = "x"
VALUE40 = {"x": 40, "doc": DOC, "matches": ["needle one", "needle two"], "read_ok": True}
VALUE42 = {**VALUE40, "x": 42}
CELLS = dict(zip(("LIFECYCLE " + name for name in ("seed", "advance", "reload", "compact", "resume", "isolation")),
                 (SEED, ADVANCE, RETAINED, RETAINED, RETAINED, ISOLATED)))


def require(condition):
    if not condition:
        raise AssertionError("lifecycle invariant failure")


def configured(rpc):
    if rpc.categories:
        # Only fixed categories can reach an exception/report, never raw diagnostics.
        raise RuntimeError("lifecycle configuration unavailable: " + ",".join(sorted(rpc.categories)))


def assert_cell(events, code, value, *, nested=False, isolated=False):
    starts = [e for e in events if e.get("type") == "tool_execution_start"]
    ends = [e for e in events if e.get("type") == "tool_execution_end"]
    python = [e for e in starts if e.get("toolName") == "python"]
    require(len(python) == 1)
    parent = python[0].get("toolCallId")
    args = python[0].get("args", {})
    require(isinstance(parent, str) and args.get("code") == code
            and args.get("operation", "execute") == "execute" and set(args) <= {"code", "operation"})
    require(len(starts) == len(ends) == (2 if nested else 1))
    require(len({e.get("toolCallId") for e in ends}) == len(ends))
    result = next((e for e in ends if e.get("toolCallId") == parent), {})
    require(result.get("toolName") == "python" and result.get("isError") is isolated)
    envelope = result.get("result", {})
    if isolated:
        require("NameError" in json.dumps(envelope.get("content")) and not envelope.get("details"))
    else:
        # JSON equality is type-sensitive (False is not integer zero).
        require(json.dumps(envelope.get("details"), sort_keys=True) == json.dumps(
            {"result": value, "output": "", "truncated": False}, sort_keys=True))
    if nested:
        read = next((e for e in starts if e.get("toolName") == "read"), {})
        read_end = next((e for e in ends if e.get("toolCallId") == parent + "/1"), {})
        require(read.get("args") == {"path": "large.txt"} and read.get("toolCallId") == parent + "/1"
                and read.get("parentToolCallId") == parent and read_end.get("toolName") == "read"
                and read_end.get("isError") is False and read_end.get("parentToolCallId") == parent)
        require("needle one" in json.dumps(read_end.get("result")) and "needle two" in json.dumps(read_end.get("result")))
    settled = [e for e in events if e.get("type") == "agent_settled"]
    require(len(settled) == 1 and settled[0].get("aborted") is False)
    return parent


def assert_usage(assistants, selector, *, replay):
    require(bool(assistants) and len(assistants) <= 2)
    for m in assistants:
        require(m.get("provider", "") + "/" + m.get("model", "") == selector
                and m.get("stopReason") in ("toolUse", "stop"))
        usage = m.get("usage", {})
        require(type(usage.get("totalTokens")) in (int, float))
        require(usage["totalTokens"] == 0 if replay else usage["totalTokens"] > 0)
        if replay:
            require(all(usage.get(k) == 0 for k in ("input", "output", "cacheRead", "cacheWrite"))
                    and all(usage.get("cost", {}).get(k) == 0 for k in ("input", "output", "cacheRead", "cacheWrite", "total")))


class LifecycleRpc(p.integration.Rpc):
    """Reuse LF pipe/deadline ownership; retain events only in memory, never log."""
    def __init__(self, proc, owned):
        super().__init__(proc, owned)
        self.events = []
        self.total_bytes = 0
        self.categories = set()

    def pump(self, deadline):
        self.owned.remaining()
        if self.eof:
            raise RuntimeError("Lifecycle RPC EOF")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Lifecycle RPC deadline")
        for key, _ in self.selector.select(min(remaining, .02)):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                self.eof = True
                require(not self.buffer)
                raise RuntimeError("Lifecycle RPC EOF")
            self.total_bytes += len(chunk)
            require(self.total_bytes <= 4 * 1024 * 1024)
            self.buffer += chunk
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeError):
                    raise RuntimeError("Lifecycle RPC invalid JSON") from None
                require(isinstance(raw, dict) and isinstance(raw.get("type"), str))
                self.categories.update(p.failure_categories(raw))
                message = p.sanitize(raw)
                if message["type"] == "response":
                    require(isinstance(message.get("id"), str) and message["id"] not in self.responses)
                    self.responses[message["id"]] = message
                else:
                    require(message["type"] in p.EVENT_TYPES)
                    self.events.append(message)
            require(len(self.buffer) <= 1024 * 1024 and len(self.events) <= 4096 and len(self.responses) <= 32)

    def request(self, request_id, request_type, timeout=10, **values):
        self.proc.stdin.write(json.dumps({"id": request_id, "type": request_type, **values}).encode() + b"\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while request_id not in self.responses:
            self.pump(deadline)
        response = self.responses.pop(request_id)
        configured(self)
        require(response.get("command") == request_type and response.get("success") is True)
        return response.get("data", {})


def hooks(path):
    require(path.stat().st_size <= 65536)
    return [json.loads(line) for line in path.read_text().splitlines()]


def prompt(rpc, phase, value, selector, log, *, replay=False, nested=False, isolated=False):
    offset = len(rpc.events)
    before = len(hooks(log))
    code = CELLS["LIFECYCLE " + phase]
    message = "LIFECYCLE " + phase + "\nUse python exactly once with this EXACT code; preserve characters and line breaks. Then reply DONE. No other direct tool or retry.\n\n" + code
    # Bounded fixture text provides an actual prefix for manual compaction.
    if phase == "seed":
        message += "\n\nFixture context (do not execute):\n" + "retained external state; " * 100
    response = rpc.request(phase, "prompt", message=message)
    require(response.get("disposition") == "started")
    deadline = time.monotonic() + (12 if replay else 30)
    while not any(e.get("type") == "agent_settled" for e in rpc.events[offset:]):
        rpc.pump(deadline)
    events = rpc.events[offset:]
    parent = assert_cell(events, code, value, nested=nested, isolated=isolated)
    if replay and not isolated:
        # Exercise the actual captured RPC events against a wrong restore oracle.
        try:
            assert_cell(events, code, {**value, "x": value["x"] + 1}, nested=nested)
        except AssertionError:
            pass
        else:
            raise AssertionError("lifecycle negative restore accepted")
    observed = hooks(log)[before:]
    calls = [e for e in observed if e.get("type") == "call"]
    results = [e for e in observed if e.get("type") == "result"]
    expected = [("python", parent)] + ([("read", parent + "/1")] if nested else [])
    require([(e.get("toolName"), e.get("id")) for e in calls] == expected and all(e.get("match") is True for e in calls))
    require([(e.get("toolName"), e.get("id"), e.get("isError")) for e in results] == (
        [("read", parent + "/1", False), ("python", parent, False)] if nested else [("python", parent, isolated)]))
    assistants = [e["message"] for e in events if e.get("type") == "message_end"
                  and e.get("message", {}).get("role") == "assistant"]
    assert_usage(assistants, selector, replay=replay)
    configured(rpc)
    return assistants


def stop(proc, rpc, owned):
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not rpc.eof:
        try:
            rpc.pump(deadline)
        except RuntimeError:
            if not rpc.eof:
                raise
    proc.wait(timeout=max(.01, deadline - time.monotonic()))
    require(proc.returncode == 0)
    deadline = time.monotonic() + 2
    while owned.remaining() and time.monotonic() < deadline:
        time.sleep(.01)
    require(not owned.remaining())


def lifecycle_check(env, cwd, installed, selector, *, replay=False):
    provider, model = selector.split("/", 1)
    log = cwd / "lifecycle.jsonl"
    (cwd / "large.txt").write_text(DOC)
    env = {**env, "PI_RLM_LIFECYCLE_LOG": str(log), "PI_RLM_LIFECYCLE_CELLS": json.dumps(CELLS)}
    argv = [env["PI_BIN"], "--mode", "rpc", "--tools", "python,read", "--provider", provider,
            "--model", model, "--thinking", "off", "--extension", str(ROOT / "lifecycle-probe.ts")]
    if replay:
        argv.extend(["--extension", str(ROOT / "lifecycle-replay-probe.ts")])
    elif env.get("PI_RLM_COMPAT_EXTENSION"):
        argv.extend(["--extension", env["PI_RLM_COMPAT_EXTENSION"]])
    assistants = []
    compaction_usage = {}
    session = None
    observed_count = 0
    for process in ("initial", "resumed"):
        command = argv + (["--session", session] if session else [])
        with tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=stderr, start_new_session=True)
            owned = p.isolation.OwnedProcesses(proc)
            rpc = LifecycleRpc(proc, owned)
            try:
                state = rpc.request("state", "get_state")
                require(state.get("model", {}).get("provider") == provider and state["model"].get("id") == model)
                require(isinstance(state.get("sessionFile"), str))
                if process == "initial":
                    session = state["sessionFile"]
                    require(Path(session).resolve().is_relative_to(Path(env["PI_CODING_AGENT_SESSION_DIR"]).resolve()))
                    assistants += prompt(rpc, "seed", VALUE40, selector, log, replay=replay, nested=True)
                    assistants += prompt(rpc, "advance", VALUE42, selector, log, replay=replay)
                    old = [e for e in hooks(log) if e.get("type") == "start"][-1]
                    require(rpc.request("reload-command", "prompt", message="/lifecycle-reload").get("disposition") == "handled")
                    fresh = [e for e in hooks(log) if e.get("type") == "start"][-1]
                    require(old["generation"] != fresh["generation"] and old["sessionFile"] == fresh["sessionFile"] == session)
                    require(rpc.request("reload-state", "get_state").get("sessionFile") == session)
                    assistants += prompt(rpc, "reload", VALUE42, selector, log, replay=replay)
                    # A context-visible custom boundary keeps the final user span intact,
                    # avoiding Pi's two-summary split-turn path in the paid budget.
                    require(rpc.request("compact-boundary", "prompt", message="/lifecycle-compact-boundary").get("disposition") == "handled")
                    compact = rpc.request("manual-compact", "compact", timeout=12 if replay else 30,
                                          customInstructions="Summarize this bounded lifecycle fixture briefly; Python state is external.")
                    require(compact.get("tokensBefore", 0) > 0 and isinstance(compact.get("summary"), str)
                            and bool(compact["summary"].strip()) and isinstance(compact.get("firstKeptEntryId"), str))
                    entries = rpc.request("entries", "get_entries").get("entries", [])
                    persisted = [e for e in entries if e.get("type") == "compaction"]
                    # RPC and on-disk transcript must both contain the same actual entry.
                    disk = [json.loads(line) for line in Path(session).read_text().splitlines()]
                    require(len(persisted) == 1 and any(e == persisted[0] for e in disk))
                    entry = persisted[0]
                    require(entry["summary"] == compact["summary"] and entry["firstKeptEntryId"] == compact["firstKeptEntryId"])
                    observed = [e for e in hooks(log) if e.get("type") == "compact"]
                    print("lifecycle compaction preparation: " + json.dumps({"attempts": len(observed),
                        "splitTurn": observed[0].get("splitTurn") is True if observed else None,
                        "smallFixtureBudgetsMatch": bool(observed) and observed[0].get("keepRecentTokens") == 256
                            and observed[0].get("reserveTokens") == 4096}), flush=True)
                    require(len(observed) == 1 and observed[0] == {"type": "compact", "reason": "manual",
                            "keepRecentTokens": 256, "reserveTokens": 4096, "splitTurn": False})
                    if replay:
                        require(entry.get("fromHook") is True and entry.get("details") == {"syntheticLifecycle": True})
                    else:
                        require(not entry.get("fromHook") and entry.get("usage", {}).get("totalTokens", 0) > 0)
                        compaction_usage = entry["usage"]
                    assistants += prompt(rpc, "compact", VALUE42, selector, log, replay=replay)
                    state_dir = Path(env["PI_RLM_STATE_DIR"]) / hashlib.sha256(session.encode()).hexdigest()
                    require(stat.S_IMODE(state_dir.stat().st_mode) == 0o700)
                    require(all(stat.S_IMODE((state_dir / name).stat().st_mode) == 0o600
                            for name in ("kernel.bin", "kernel.names.json")))
                else:
                    require(state["sessionFile"] == session)
                    assistants += prompt(rpc, "resume", VALUE42, selector, log, replay=replay)
                    require(rpc.request("new", "new_session").get("cancelled") is False)
                    different = rpc.request("new-state", "get_state").get("sessionFile")
                    require(isinstance(different, str) and different != session)
                    assistants += prompt(rpc, "isolation", None, selector, log, replay=replay, isolated=True)
                sidecar = str(installed / "packages/rlm-kernel/python/sidecar.py").encode()
                require(any(sidecar in Path(f"/proc/{pid}/cmdline").read_bytes() for pid in owned.remaining()))
                stop(proc, rpc, owned)  # Zero owned identities BEFORE a resumed process can launch.
                observed_count += len(owned.observed)
            finally:
                owned.cleanup()
                rpc.close()
                if not proc.stdin.closed:
                    proc.stdin.close()
                proc.stdout.close()
    actual_calls = [e for e in hooks(log) if e.get("type") == "call"]
    require(sum(e.get("toolName") == "python" for e in actual_calls) == 6
            and sum(e.get("toolName") == "read" for e in actual_calls) == 1)
    require(6 <= len(assistants) <= 12)
    usage = safe_usage([m["usage"] for m in assistants] + ([compaction_usage] if compaction_usage else []))
    print("lifecycle PASS: " + json.dumps({"mode": "offline-local-replay" if replay else "live-configured-litellm",
        "pythonQueries": 6, "assistantResponses": len(assistants), "nestedReads": 1, "compactions": 1,
        "compactionSource": "synthetic-hook-NOT-paid-proof" if replay else "actual-provider",
        "phases": {name: "PASS" for name in ("seed40", "advance42", "reload", "compact", "resume", "isolation")},
        "ownedProcesses": observed_count, "survivorsBeforeResume": 0, "survivorsBeforeBackup": 0,
        "usage": usage, "costLabel": "provider-reported estimate; small fixture is not performance savings",
        "acceptedRisks": {"576": "closed-accepted-risk-not-fixed", "IUA": "open-P3-nonblocking-unknown"}}, sort_keys=True), flush=True)


def safe_usage(values):
    result = {}
    for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens"):
        numbers = [value.get(key, 0) for value in values]
        require(all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1e9 for n in numbers))
        result[key] = sum(numbers)
    costs = [value.get("cost", {}).get("total", 0) for value in values]
    require(all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1e9 for n in costs))
    result["estimatedCost"] = sum(costs)
    return result


def worker(selector, *, replay=False):
    require(not p.isolation.children(os.getpid()))
    env = {**os.environ, "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cwd = Path.cwd()
    with p.isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-lifecycle-source-") as directory:
        fixture = Path(directory)
        original, installed = fixture / "original", fixture / "installed"
        command = lambda *argv: p.installation.command(env, cwd, *argv)
        sha = command("git", "-C", p.installation.REPO, "rev-parse", "HEAD")
        command("git", "clone", "--no-hardlinks", "--no-checkout", p.installation.REPO, original)
        command("git", "-C", original, "checkout", "--detach", sha)
        command("git", "clone", "--no-hardlinks", original, installed)
        command("git", "-C", installed, "checkout", "--detach", sha)
        command("git", "-C", installed, "remote", "remove", "origin")
        p.installation.committed_source(env, cwd, installed, sha)
        original.rename(fixture / "unavailable-original")
        require(not original.exists())
        agent = Path(env["PI_CODING_AGENT_DIR"])
        settings = agent / "settings.json"
        settings.write_text(json.dumps({"extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"],
            "retry": {"enabled": False}, "compaction": {"enabled": False, "keepRecentTokens": 256, "reserveTokens": 4096}, "cacheWarming": "off"}))
        settings.chmod(0o600)
        installer = env["_PI_ISOLATED_EXECUTABLE"]
        command(installer, "install", installed)
        configured = json.loads(settings.read_text())
        packages = configured.get("packages", [])
        require(len(packages) == 1 and isinstance(packages[0], str) and (agent / packages[0]).resolve() == installed)
        listing = command(installer, "list")
        require(str(installed) in listing or packages[0] in listing)
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm-kernel/index.ts"], "skills": [], "prompts": [], "themes": []}]
        settings.write_text(json.dumps(configured))
        if not replay:
            identity = p.configure_provider(agent, selector, Path(env["PI_RLM_PROVIDER_PACKAGE"]))
            require(identity["name"] == "pi-provider-litellm")
        if replay:
            p.integration.protocol_checks(env, cwd)
        print("lifecycle source: " + sha + "; installed independent committed clone; original fixture unavailable", flush=True)
        lifecycle_check(env, cwd, installed, selector, replay=replay)
    require(not fixture.exists())
    print("lifecycle owned source fixtures: absent", flush=True)


def run(selector, *, replay=False):
    require(selector == REPLAY_SELECTOR if replay else selector.startswith("litellm/") and bool(selector.split("/", 1)[1]))
    # Live-only source lookup/copy. Offline and absent-model paths never enter it.
    source = None if replay else Path(os.environ.get("PI_CODING_AGENT_DIR", str(Path.home() / ".pi/agent")))
    with p.isolation.isolated_environment(discover_extensions=True) as env:
        agent = Path(env["PI_CODING_AGENT_DIR"])
        command = [sys.executable, str(ROOT / "e2e.py"), "--_worker", "--model", selector]
        if replay:
            command.append("--_replay")
            result = p.isolation.run_owned_capture(command, cwd=agent.parent / "cwd", env=env, timeout=36)
        else:
            env["PI_RLM_PROVIDER_PACKAGE"] = str(source / "npm/node_modules/pi-provider-litellm")
            compatibility = source / "extensions/litellm-bedrock-compat.ts"
            if compatibility.is_file():
                env["PI_RLM_COMPAT_EXTENSION"] = str(compatibility)
            with p.private_models(source, agent), p.private_litellm_auth(source, agent):
                result = p.isolation.run_owned_capture(command, cwd=agent.parent / "cwd", env=env, timeout=150)
        # Workers use only fixed failures; no raw diagnostics/transcripts leave them.
        print(result.stdout, end="")
        # Supervisor diagnostics contain owned counts/status, not provider stderr.
        print(result.stderr, end="", file=sys.stderr)
    require(not agent.parent.exists())
    print("lifecycle private agent/config/cwd/session/state: absent; " + (
          "no real source config read" if replay else "source auth byte-equal; selected-only private auth/model copies absent"), flush=True)
    return result.returncode
