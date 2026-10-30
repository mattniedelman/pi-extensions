#!/usr/bin/env python3
"""No-inference real-pi permission replay; standalone Linux/pi 1.1.0 proof."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import permissions as p

ROOT = Path(__file__).resolve().parent
SELECTOR = "q1s-local-replay/fixed-python"
CASES = ("good", "readBeforeDeny", "lostHook")
SOT_CHECKS = frozenset({
    "settled", "python_call_count", "actual_python_arguments", "execution_counts", "nested_ids",
    "nested_arguments", "nested_parent", "execution_ends", "nested_status", "allowed_content",
    "denial_reason", "python_status", "python_result_envelope", "hook_calls", "hook_ids",
    "hook_arguments", "hook_denial_reason", "hook_results", "selected_model", "model_status",
    "replay_zero_inference", "rpc_privacy", "rpc_protocol", "rpc_event_types", "state_response",
    "state_model", "prompt_response", "installed_sidecar", "orderly_shutdown", "owned_before_backup",
    "stderr_privacy", "hook_privacy", "hook_protocol", "observation_errors",
})
PREFLIGHT_BLOCKED = "BLOCKED: approved offline Jupyter prerequisites unavailable; preparation/download approval required"


class PreflightUnavailable(RuntimeError):
    exit_code = 69


def jupyter_preflight(cache, *, executable="uv", root=None, extra_env=None):
    """Materialize the locked sidecar environment offline under owned supervision."""
    disposable = Path(root) if root else Path(tempfile.mkdtemp(prefix="pi-q1s-jupyter-preflight-"))
    try:
        disposable.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = disposable / "environment"
        env = {**os.environ, "UV_OFFLINE": "1", "UV_CACHE_DIR": str(cache),
               "UV_PROJECT_ENVIRONMENT": str(environment), **(extra_env or {})}
        try:
            result = p.isolation.run_owned_capture([str(executable), "sync", "--project", str(ROOT / "python"),
                "--locked", "--no-dev"], cwd=ROOT, env=env, timeout=15)
        except Exception:
            raise PreflightUnavailable(PREFLIGHT_BLOCKED) from None
        if result.returncode or not (environment / "bin/python").is_file():
            raise PreflightUnavailable(PREFLIGHT_BLOCKED)
        print("preflight approved offline Jupyter prerequisites: PASS; disposable environment materialized", flush=True)
    finally:
        shutil.rmtree(disposable, ignore_errors=True)
        if disposable.exists():
            raise RuntimeError("preflight disposable root survived")

# Loaded by Pi solely to acquire its real pi-ai stream helper; no agent/model call.
ABORT_PROBE = """
import assert from "node:assert/strict";
import { appendFileSync } from "node:fs";
import { createAssistantMessageEventStream, getCurrentTools } from "@earendil-works/pi-ai";
export default function (pi) {
  let requests = 0;
  pi.registerTool({ name: "wait_signal", label: "Wait for signal", description: "Abort ownership fixture",
    parameters: { type: "object", properties: {}, additionalProperties: false },
    async execute(id, args, signal) {
      assert.equal(id, "call_" + "a".repeat(32) + "/1");
      assert.deepEqual(args, {});
      const aborted = new Promise(resolve => signal.addEventListener("abort", resolve, { once: true }));
      appendFileSync(process.env.PI_RLM_ABORT_LOG, "nested-start\\n");
      await aborted;
      appendFileSync(process.env.PI_RLM_ABORT_LOG, "nested-signal\\n");
      throw signal.reason instanceof Error ? signal.reason : new Error("aborted");
    } });
  pi.registerProvider("q1s-abort-replay", { api: "q1s-abort-api", baseUrl: "local://q1s-no-network",
    apiKey: "q1s-test-only-not-a-credential", models: [{ id: "fixed-python", name: "Abort replay",
      reasoning: false, input: ["text"], contextWindow: 32768, maxTokens: 1024,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple(model, context, options) {
      assert.equal(++requests, 1);
      assert.ok(getCurrentTools(context.messages).some(tool => tool.name === "python"));
      const stream = createAssistantMessageEventStream();
      const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
        content: [], timestamp: Date.now(), stopReason: "pending", usage: { input: 0, output: 0,
          cacheRead: 0, cacheWrite: 0, totalTokens: 0,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      (async () => { try {
        const call = { type: "toolCall", id: "call_" + "a".repeat(32), name: "python",
          arguments: { code: "pi.wait_signal()" } };
        message.content.push(call);
        for (const event of [{ type: "start", partial: message },
          { type: "toolcall_start", contentIndex: 0, partial: message },
          { type: "toolcall_delta", contentIndex: 0, delta: JSON.stringify(call.arguments), partial: message },
          { type: "toolcall_end", contentIndex: 0, toolCall: call, partial: message }]) {
          if (options.signal.aborted) throw Error("aborted");
          stream.push(event);
        }
        message.stopReason = "toolUse";
        stream.push({ type: "done", reason: "toolUse", message });
      } catch { message.stopReason = "aborted"; stream.push({ type: "error", reason: "aborted", error: message }); }
      finally { stream.end(); } })();
      return stream;
    } });
}
"""

STREAM_PROBE = """
import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";
import factory from REPLAY_FACTORY;
export default pi => pi.registerCommand("q1s-stream-test", {
  description: "Offline local replay stream contract", handler: async () => {
    const cases = [];
    const context = { messages: [{ role: "system", content: "", timestamp: 0,
      toolsAdded: ["python", "read"].map(name => ({ name, description: "fixture", parameters: {} })) }] };
    const fresh = () => {
      let config;
      factory({ on() {}, registerProvider(name, value) {
        assert.equal(name, "q1s-local-replay"); config = value;
      } });
      return { config, model: { ...config.models[0], provider: "q1s-local-replay", api: config.api } };
    };
    const collect = async (provider, options = {}, transcript = context) => {
      const stream = provider.config.streamSimple(provider.model, transcript, options);
      const events = [];
      for await (const event of stream) events.push(structuredClone(event));
      const result = await stream.result();
      const terminal = events.filter(event => ["done", "error"].includes(event.type));
      assert.equal(terminal.length, 1);
      assert.equal(events.at(-1), terminal[0]);
      assert.equal(stream.done, true);
      assert.equal(result.stopReason, terminal[0].reason);
      assert.equal(result.provider + "/" + result.model, "q1s-local-replay/fixed-python");
      assert.deepEqual(Object.values(result.usage.cost), [0, 0, 0, 0, 0]);
      assert.deepEqual(Object.entries(result.usage).filter(([key]) => key !== "cost").map(([, value]) => value), [0, 0, 0, 0, 0]);
      return { events, result, terminal: terminal[0] };
    };
    const check = async (name, action) => {
      try { const result = await action(); cases.push({ name, passed: true, ...result }); }
      catch (error) { cases.push({ name, passed: false, error: error.message }); }
    };
    const normal = fresh();
    for (const [name, block, reason] of [["normalTool", "toolcall", "toolUse"], ["normalText", "text", "stop"]]) {
      await check(name, async () => {
        const { events, result, terminal } = await collect(normal);
        assert.deepEqual(events.map(event => event.type), ["start", block + "_start", block + "_delta", block + "_end", "done"]);
        assert.equal(terminal.reason, reason);
        assert.equal(result.content.length, 1);
        if (block === "toolcall") assert.deepEqual(result.content[0].arguments, { code: process.env.PI_RLM_EXPECTED_CELL });
        else assert.equal(result.content[0].text, "DONE");
        return { terminal: terminal.type, reason, events: events.length, closed: true };
      });
    }
    const faults = [
      ["preAbort", "pre"], ["payloadAbort", "payload"], ["streamStartAbort", "start"],
      ["toolStartAbort", "toolcall_start"], ["toolDoneAbort", "done"],
      ["textStartAbort", "text_start", true], ["textDoneAbort", "done", true],
      ["payloadThrow", "payloadThrow"], ["instrumentThrow", "instrumentThrow"],
      ["missingTools", "missingTools"], ["thirdRequest", "thirdRequest"],
    ];
    for (const [name, fault, text] of faults) {
      await check(name, async () => {
        const provider = fault === "thirdRequest" ? normal : fresh();
        if (text) await collect(provider);
        const controller = new AbortController();
        const observed = [];
        if (fault === "pre") controller.abort();
        const { events, result, terminal } = await collect(provider, {
          signal: controller.signal,
          onPayload: async () => {
            await Promise.resolve();
            if (fault === "payload") controller.abort();
            if (fault === "payloadThrow") throw Error("fixture payload failure");
          },
          onProviderStreamEvent: async event => {
            observed.push(event.type);
            await Promise.resolve();
            if (event.type === fault) controller.abort();
            if (fault === "instrumentThrow") throw Error("fixture instrumentation failure");
          },
        }, fault === "missingTools" ? { messages: [] } : context);
        const reason = name.endsWith("Abort") ? "aborted" : "error";
        assert.equal(terminal.type, "error", name + " returned " + terminal.type + "/" + terminal.reason);
        assert.equal(terminal.reason, reason);
        assert.equal(events.some(event => event.type === "done"), false);
        assert.equal(typeof result.errorMessage, "string");
        if (["pre", "payload"].includes(fault)) assert.deepEqual(events.map(event => event.type), ["error"]);
        if (controller.signal.aborted && observed.length) {
          assert.equal(observed.at(-1), fault);
          assert.equal(events.some(event => event.type === fault), false, "interrupted event was pushed");
        }
        return { terminal: terminal.type, reason, events: events.length, closed: true };
      });
    }
    writeFileSync(process.env.PI_RLM_STREAM_OUTPUT, JSON.stringify(cases));
  },
});
"""


def abort_stage(stage, action):
    """Run one abort boundary and retain its fixed stage name on failure."""
    try:
        return action()
    except BaseException as error:
        detail = str(error).replace("\n", " ")[:500]
        suffix = f": {detail}" if detail else ""
        raise AssertionError(f"abort stage {stage}: {p.safe_error_type(error)}{suffix}") from error


def assert_abort_events(events, parent):
    nested = parent + "/1"
    starts = [e for e in events if e.get("type") == "tool_execution_start"]
    ends = [e for e in events if e.get("type") == "tool_execution_end"]
    settled = [e for e in events if e.get("type") == "agent_settled"]
    start_shape = [(e.get("toolName"), e.get("toolCallId"), e.get("parentToolCallId")) for e in starts]
    assert start_shape == [("python", parent, None), ("wait_signal", nested, parent)], \
        "abort stage event-shape: unexpected start events " + repr(start_shape)
    assert len(settled) == 1 and settled[0].get("aborted") is True, \
        "abort stage settled: expected one aborted settlement, got " + repr(settled)
    end_ids = [e.get("toolCallId") for e in ends]
    assert len(ends) == 2 and set(end_ids) == {parent, nested}, \
        "abort stage end-events: expected parent+nested exactly once, got " + repr(ends)
    ends_by_id = {e.get("toolCallId"): e for e in ends}
    nested_end, parent_end = ends_by_id[nested], ends_by_id[parent]
    nested_result = json.dumps(nested_end.get("result"), sort_keys=True).lower()
    assert nested_end.get("isError") is True and "abort" in nested_result, \
        "abort stage nested-result: " + repr(nested_end)
    assert "late-result" not in nested_result, "abort stage late-result: " + repr(nested_end)
    assert parent_end.get("isError") is True, "abort stage parent-result: " + repr(parent_end)
    settled_at = events.index(settled[0])
    late = [e for e in events[settled_at + 1:]
            if e.get("type") in ("tool_execution_start", "tool_execution_end")]
    assert not late, "abort stage post-settlement-events: " + repr(late)


def final_abort_events(proc, rpc, owned, parent):
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not rpc.eof:
        try:
            rpc.pump(deadline)
        except RuntimeError:
            if not rpc.eof:
                raise
    proc.wait(timeout=max(0, deadline - time.monotonic()))
    events = list(rpc.events)
    assert_abort_events(events, parent)
    remaining = owned.remaining()
    assert proc.returncode == 0, "abort stage process-exit: " + repr(proc.returncode)
    assert not remaining, "abort stage process-cleanup: " + repr(remaining)
    return events


def abort_check(env, cwd):
    probe, log = cwd / "abort-probe.ts", cwd / "abort.log"
    probe.write_text(ABORT_PROBE)
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--tools", "python,wait_signal",
            "--provider", "q1s-abort-replay", "--model", "fixed-python", "--thinking", "off",
            "--extension", str(probe)]
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd, env={**env, "PI_RLM_ABORT_LOG": str(log)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        owned = p.isolation.OwnedProcesses(proc)
        rpc = p.LiveRpc(proc, owned, "Q1S_SENTINEL_NOT_PRESENT")
        try:
            state = abort_stage("state", lambda: rpc.request("state", "get_state"))
            assert state.get("success") is True, "abort stage state: unsuccessful response"
            response = abort_stage("prompt", lambda: rpc.request(
                "permissions", "prompt", message="Run the requested Python fixture."))
            assert response.get("success") is True and response.get("data", {}).get("disposition") == "started", \
                "abort stage prompt: request did not start"
            abort_stage("nested-ready", lambda: p.integration.wait_until(
                lambda: log.exists() and log.read_text().splitlines() == ["nested-start"], rpc, owned))
            abort = abort_stage("request", lambda: rpc.request("abort", "abort"))
            assert abort.get("success") is True, "abort stage request: unsuccessful response"
            abort_stage("settled", lambda: p.integration.wait_until(
                lambda: any(e.get("type") == "agent_settled" for e in rpc.events), rpc, owned))
            events = abort_stage("final-drain", lambda: final_abort_events(
                proc, rpc, owned, "call_" + "a" * 32))
            signal_log = log.read_text().splitlines()
            assert signal_log == ["nested-start", "nested-signal"], \
                "abort stage signal-log: " + repr(signal_log)
            assistants = [e.get("message") for e in events if e.get("type") == "message_end"
                and isinstance(e.get("message"), dict) and e["message"].get("role") == "assistant"]
            usage_ok = all(sum(m.get("usage", {}).get(k, 0) for k in
                ("input", "output", "cacheRead", "cacheWrite", "totalTokens")) == 0 for m in assistants)
            assert usage_ok, "abort stage usage: nonzero inference accounting"
            print("registered abort: parent+nested signal owned; late attachment 0; 0 survivors", flush=True)
        except BaseException as error:
            message = str(error)
            if message.startswith("abort stage "):
                print(message, file=sys.stderr, flush=True)
            raise
        finally:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), end="", file=sys.stderr)
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
            probe.unlink(missing_ok=True)
            log.unlink(missing_ok=True)
    assert not p.isolation.children(os.getpid()), "abort probe children survived finally"


def stream_check(env, cwd):
    probe, output = cwd / "stream-probe.ts", cwd / "stream-result.json"
    probe.write_text(STREAM_PROBE.replace("REPLAY_FACTORY", json.dumps(str(ROOT / "permissions-replay-probe.ts"))))
    argv = [env["PI_BIN"], "--mode", "rpc", "--no-session", "--extension", str(probe)]
    with tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(argv, cwd=cwd,
            env={**env, "PI_RLM_REPLAY_CASE": "good", "PI_RLM_EXPECTED_CELL": p.CELL,
                 "PI_RLM_STREAM_OUTPUT": str(output)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, start_new_session=True)
        owned = p.isolation.OwnedProcesses(proc)
        rpc = p.integration.Rpc(proc, owned)
        try:
            assert rpc.request("state", "get_state")["success"] is True
            assert rpc.request("probe", "prompt", message="/q1s-stream-test")["success"] is True
            p.integration.wait_until(output.exists, rpc, owned)
            cases = json.loads(output.read_text())
            proc.stdin.close()
            proc.wait(timeout=5)
            assert proc.returncode == 0, "stream probe host failed"
            assert not owned.remaining(), "stream probe survivors BEFORE backup"
            print("stream host: exit0; 0 survivors BEFORE backup", flush=True)
            print("replay stream contract: " + json.dumps(cases, sort_keys=True), flush=True)
            assert len(cases) == 13 and all(case["passed"] for case in cases), "replay stream contract failure"
        finally:
            stderr.seek(0)
            print(stderr.read().decode(errors="replace"), end="", file=sys.stderr)
            owned.cleanup()
            rpc.close()
            if not proc.stdin.closed:
                proc.stdin.close()
            proc.stdout.close()
            probe.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
    assert not p.isolation.children(os.getpid()), "stream probe children survived finally"
    print("stream finally: 0 owned children; loader/results absent; 13/13 contract checks PASS", flush=True)


def sot_verdict(report):
    """Judge registered ownership evidence without inode sensor timing."""
    checks = report.get("checks", {})
    return set(checks) >= SOT_CHECKS and all(checks.get(name) == "PASS" for name in SOT_CHECKS)


def replay_case(env, cwd, installed, case, *, sot=False):
    (cwd / "policy.jsonl").unlink(missing_ok=True)
    output = io.StringIO()
    assertion = None
    with contextlib.redirect_stdout(output):
        try:
            p.live_check({**env, "PI_RLM_REPLAY_CASE": case}, cwd, installed, SELECTOR, replay=True)
        except AssertionError as error:
            assertion = str(error)
    text = output.getvalue()
    assert "IUA_PRIVATE_" not in text, "unsafe replay report"
    lines = [line for line in text.splitlines() if line.startswith("permissions live ")]
    assert len(lines) == 1, "complete replay diagnostics missing"
    report = json.loads(lines[0].split(": ", 1)[1])
    print("permissions replay " + case + " " + ("PASS: " if report["passed"] else "FAIL: ") +
          json.dumps(report, sort_keys=True), flush=True)
    failed = {name for name, value in report["checks"].items() if value == "FAIL"}
    assert set(report["sensorPhases"]) == set(p.PHASES), "replay sensor phases missing"
    assert len(report["checks"]) == 36, "replay check count changed"
    assert report["survivorsBeforeBackup"] == 0, "owned survivors BEFORE backup"
    assert not p.isolation.children(os.getpid()), "replay children survived finally cleanup"
    print("replay finally " + case + ": 0 owned children; 0 survivors BEFORE backup", flush=True)
    if sot:
        assert case == "good", "SOT inventory must contain only the registered ownership case"
        assert sot_verdict(report) and (assertion is None or assertion == "permissions invariant failure"), \
            "registered ownership replay rejected"
        ignored = {name for name in failed if name not in SOT_CHECKS}
        assert failed <= {"sensor", "sensor_monotonic"}, "non-ownership failure escaped SOT: " + repr(failed)
        print("SOT registered ownership PASS: 34/34 ownership checks; sensor timing excluded: " +
              json.dumps(sorted(ignored)), flush=True)
    elif case == "good":
        assert report["passed"] and assertion is None and not failed, "real callback replay rejected"
    else:
        assert not report["passed"] and assertion == "permissions invariant failure", "negative strict verdict lost"
        assert report["sensorPhases"]["afterSettled"]["denied.txt"] > 0, "negative actual denied IO missing"
        if case == "readBeforeDeny":
            assert failed == {"sensor"}, "read-before-denial negative did not isolate sensor"
        else:
            assert {"sensor", "rpc_privacy", "hook_results", "python_result_envelope"} <= failed, "lost hook escaped oracle"
        print("replay negative " + case + ": expected strict FAIL discriminated actual denied IO", flush=True)


def inventory(*, sot=False):
    return (not sot, ("good",) if sot else CASES, sot)


def worker(*, sot=False):
    if p.isolation.children(os.getpid()):
        raise RuntimeError("replay worker must start child-free")
    env = {**os.environ, "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cwd = Path.cwd()
    command = lambda *argv: p.installation.command(env, cwd, *argv)
    run_stream, cases, run_abort = inventory(sot=sot)
    with p.isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-q1s-source-") as directory:
        if run_stream:
            stream_check(env, cwd)
        fixture = Path(directory)
        installed = fixture / "installed"
        runtime = ("package.json", *("packages/rlm/" + name for name in p.integration.RUNTIME_FILES))
        for relative in runtime:
            source = p.installation.REPO / relative
            target = installed / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        installed = p.integration.independent_source(installed)
        provenance = {relative: command("git", "hash-object", p.installation.REPO / relative) for relative in runtime}
        copied = {relative: command("git", "hash-object", installed / relative) for relative in runtime}
        assert copied == provenance, "current-source snapshot provenance mismatch"
        print("replay current source provenance: " + json.dumps(provenance, sort_keys=True), flush=True)
        settings = Path(env["PI_CODING_AGENT_DIR"]) / "settings.json"
        settings.write_text(json.dumps({"extensions": ["-builtin:mcp", "-builtin:llama.cpp", "-builtin:codemode", "-builtin:tool-search"],
            "retry": {"enabled": False}, "compaction": {"enabled": False}, "cacheWarming": "off"}))
        installer = env["_PI_ISOLATED_EXECUTABLE"]
        command(installer, "install", installed)
        configured = json.loads(settings.read_text())
        packages = configured.get("packages", [])
        assert len(packages) == 1 and isinstance(packages[0], str), "actual installer declaration missing"
        assert (settings.parent / packages[0]).resolve() == installed, "wrong installed package"
        command(installer, "list")
        configured["packages"] = [{"source": packages[0], "extensions": ["+packages/rlm/index.ts"],
                                   "skills": [], "prompts": [], "themes": []}]
        settings.write_text(json.dumps(configured))
        settings.chmod(0o600)
        print("replay current source: independent byte snapshot; original worktree not installed", flush=True)
        for case in cases:
            replay_case(env, cwd, installed, case, sot=sot)
        if run_abort:
            abort_check(env, cwd)
        if sot:
            print("SOT ownership inventory PASS: registered-good, registered-abort; sensor/denial mutants excluded", flush=True)
    assert not fixture.exists(), "owned source fixture survived"
    print("replay owned committed source fixtures: absent", flush=True)


def preflight_controls(cache):
    with tempfile.TemporaryDirectory(prefix="pi-q1s-preflight-control-") as directory:
        root = Path(directory)
        empty_cache, disposable = root / "empty-cache", root / "disposable"
        empty_cache.mkdir()
        attempt = root / "attempt"
        fake = root / "uv"
        fake.write_text("#!/bin/sh\nprintf x >> \"$PI_RLM_UV_ATTEMPT\"\nexit 7\n")
        fake.chmod(0o700)
        try:
            jupyter_preflight(empty_cache, executable=fake, root=disposable,
                extra_env={"PI_RLM_UV_ATTEMPT": str(attempt)})
        except PreflightUnavailable as error:
            assert error.exit_code == 69 and str(error) == PREFLIGHT_BLOCKED
        else:
            raise AssertionError("negative preflight unexpectedly succeeded")
        assert attempt.read_text() == "x", "negative preflight did not make exactly one uv attempt"
        assert not disposable.exists(), "negative preflight disposable root survived"
        print("preflight negative: exit69 exact BLOCKED; replay starts 0; uv attempts 1; online fallback 0; disposable absent", flush=True)
    jupyter_preflight(cache)


def run(*, stream_only=False, sot=False):
    cache = Path(os.environ.get("UV_CACHE_DIR", ""))
    try:
        preflight_controls(cache)
    except PreflightUnavailable as error:
        print(str(error), file=sys.stderr, flush=True)
        return error.exit_code
    # Outside all dedicated ownership supervisors: it must never be signaled.
    with tempfile.TemporaryDirectory(prefix="pi-q1s-unrelated-") as directory:
        root = Path(directory)
        sentinel = subprocess.Popen([sys.executable, "-c", "import signal; print('ready',flush=True); signal.pause()"],
            env={"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root)},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            assert sentinel.stdout.readline() == "ready\n", "unrelated sentinel not ready"
            with p.isolation.isolated_environment(discover_extensions=True) as env:
                agent = Path(env["PI_CODING_AGENT_DIR"])
                worker_arg = "--_stream-worker" if stream_only else "--_sot-worker" if sot else "--_worker"
                result = p.isolation.run_owned_capture([sys.executable, str(ROOT / "permissions_replay.py"), worker_arg],
                    cwd=agent.parent / "cwd", env=env, timeout=14 if stream_only else 50)
            print(result.stdout, end="")
            print(result.stderr, end="", file=sys.stderr)
            assert not agent.parent.exists(), "private agent/cwd/state/session root survived"
            assert sentinel.poll() is None, "unrelated sentinel signaled"
            print("replay private agent/cwd/state/session/source: absent; unrelated sentinel alive", flush=True)
            return result.returncode
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=2)
            sentinel.stdout.close()


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--_worker"]:
            worker()
        elif sys.argv[1:] == ["--_sot-worker"]:
            worker(sot=True)
        elif sys.argv[1:] == ["--_stream-worker"]:
            assert not p.isolation.children(os.getpid()), "stream worker must start child-free"
            with p.isolation.subreaper():
                stream_check(dict(os.environ), Path.cwd())
        else:
            assert sys.argv[1:] in ([], ["--stream-test"], ["--sot"]), "unsupported replay argv"
            sys.exit(run(stream_only=sys.argv[1:] == ["--stream-test"], sot=sys.argv[1:] == ["--sot"]))
    except Exception as error:
        print("permissions replay failure: " + p.safe_error_type(error), file=sys.stderr, flush=True)
        sys.exit(1)
