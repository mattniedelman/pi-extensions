#!/usr/bin/env python3
"""No-inference real-pi permission replay; standalone Linux/pi 1.1.0 proof."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import permissions as p

ROOT = Path(__file__).resolve().parent
SELECTOR = "q1s-local-replay/fixed-python"
CASES = ("good", "readBeforeDeny", "lostHook")

# Loaded by Pi solely to acquire its real pi-ai stream helper; no agent/model call.
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


def replay_case(env, cwd, installed, case):
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
    if case == "good":
        assert report["passed"] and assertion is None and not failed, "real callback replay rejected"
    else:
        assert not report["passed"] and assertion == "permissions invariant failure", "negative strict verdict lost"
        assert report["sensorPhases"]["afterSettled"]["denied.txt"] > 0, "negative actual denied IO missing"
        if case == "readBeforeDeny":
            assert failed == {"sensor"}, "read-before-denial negative did not isolate sensor"
        else:
            assert {"sensor", "rpc_privacy", "hook_results", "python_result_envelope"} <= failed, "lost hook escaped oracle"
        print("replay negative " + case + ": expected strict FAIL discriminated actual denied IO", flush=True)


def worker():
    if p.isolation.children(os.getpid()):
        raise RuntimeError("replay worker must start child-free")
    env = {**os.environ, "UV_OFFLINE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    cwd = Path.cwd()
    command = lambda *argv: p.installation.command(env, cwd, *argv)
    with p.isolation.subreaper(), tempfile.TemporaryDirectory(prefix="pi-q1s-source-") as directory:
        stream_check(env, cwd)
        fixture = Path(directory)
        original, installed = fixture / "original", fixture / "installed"
        sha = command("git", "-C", p.installation.REPO, "rev-parse", "HEAD")
        command("git", "clone", "--no-hardlinks", "--no-checkout", p.installation.REPO, original)
        command("git", "-C", original, "checkout", "--detach", sha)
        command("git", "clone", "--no-hardlinks", original, installed)
        command("git", "-C", installed, "checkout", "--detach", sha)
        command("git", "-C", installed, "remote", "remove", "origin")
        p.installation.committed_source(env, cwd, installed, sha)
        original.rename(fixture / "unavailable-original")
        assert not original.exists(), "original fixture remained available"
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
        print("replay committed source: " + sha + "; independent clone; original fixture unavailable", flush=True)
        for case in CASES:
            replay_case(env, cwd, installed, case)
    assert not fixture.exists(), "owned source fixture survived"
    print("replay owned committed source fixtures: absent", flush=True)


def run(*, stream_only=False):
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
                result = p.isolation.run_owned_capture([sys.executable, str(ROOT / "permissions_replay.py"),
                    "--_stream-worker" if stream_only else "--_worker"],
                    cwd=agent.parent / "cwd", env=env, timeout=14 if stream_only else 36)
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
        elif sys.argv[1:] == ["--_stream-worker"]:
            assert not p.isolation.children(os.getpid()), "stream worker must start child-free"
            with p.isolation.subreaper():
                stream_check(dict(os.environ), Path.cwd())
        else:
            assert sys.argv[1:] in ([], ["--stream-test"]), "unsupported replay argv"
            sys.exit(run(stream_only=bool(sys.argv[1:])))
    except Exception as error:
        print("permissions replay failure: " + p.safe_error_type(error), file=sys.stderr, flush=True)
        sys.exit(1)
