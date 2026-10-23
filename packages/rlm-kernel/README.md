# rlm-kernel

A pi extension exposing one model-only `python` tool backed by a persistent,
sandboxed Python-subset Monty session in a Python sidecar. It is part of the
[root pi package](../../README.md). The [v1 decision record](../../changes/ios.1-rlm-kernel-v1/decisions/001-preserve-the-pi-owned-monty-runtime.md)
explains the ownership and compatibility boundaries.

## Support and bootstrap

The supported v1 target is Linux with pi 1.1.0. The bounded suite was tested on
Linux x86_64 with Node 26.11.1, CPython 3.13.16 and uv 0.12.23. The manifest's
Node >=22 and sidecar's Python >=3.10 are dependency constraints, not a tested
version matrix. No blanket compatibility with earlier/later pi or other operating
systems is promised. This extension uses pi 1.1.0's `model-only` exposure,
`ctx.tools`, `ctx.executeTool` and session lifecycle APIs.

`uv` must be on `PATH`. At `session_start` (not first tool use), the extension
spawns `uv run --project <package>/packages/rlm-kernel/python --locked python
<package>/packages/rlm-kernel/python/sidecar.py`. uv creates/synchronizes the shared
`<state-root>/.venv` using [pyproject.toml](python/pyproject.toml) and
[uv.lock](python/uv.lock). Monty, its client and runtime are pinned to 1.1.0;
typing-extensions is locked to 4.16.0. `--locked` checks the dependency lock; it
does not pin the Python interpreter. The first tool request waits for readiness.

A cold bootstrap can download Python/dependencies and requires registry/cache
access. Pi's `--offline` flag does not make uv offline. Prewarm dependencies with
the same locked project and `UV_PROJECT_ENVIRONMENT=<state-root>/.venv` if needed;
`UV_OFFLINE=1` can enforce cache-only uv operation, but missing cached dependencies
then fail. Missing uv, failed downloads, incompatible Python/wheels or invalid
locks are startup errors, not a working empty kernel. The client has a 120-second
startup deadline. Inspect the reported error before retrying. After fixing the
cause, restart/reload the session; requests can also restart a closed sidecar.

The venv is shared by sessions under a state root. Do not delete or resynchronize
it while any of those sessions are running. For a damaged environment, stop all
such sessions, remove only the venv as described under cleanup, and bootstrap
again from the reviewed locked package. pi supplies its host packages and
`typebox`; they are unbundled `"*"` peers, not additional runtime installations.

## Pi tools from Python

Callable pi tools appear as natural methods inside the sandbox:

```python
readme = pi.read(path="README.md")
status = pi.bash(command="git status")
```

Use `pi.call("exact-tool-name", {...})` for collisions, reserved names or names
without a natural method. Use `pi.tools()`, `pi.search(query)` (up to 20 matches),
and `pi.describe(name)` for discovery. Methods resolve against the current
callable pi tool context and execute through `ctx.executeTool()`: validation,
permission hooks, abort propagation, usage accounting and nested-call records
stay owned by pi. The bridge does not create a separate permission system.

Each call returns `content`, `structured`, and `is_error`. Non-text content,
including images, is unsupported. JSON result envelopes larger than 1 MiB return
an error rather than silently truncating the data for analysis. Tool failures
return `is_error`, so Python can inspect structured error data. Transport failures
raise a Python `RuntimeError`. Calls to `python` and `codemode` are rejected to
prevent recursive orchestration.

Python state rolls back when execution fails; completed pi-tool effects do not.
For example, a successful `pi.write(...)` remains on disk even if later Python
code raises an exception. A request timeout or abort may kill the sidecar; a
subsequent request starts it again and restores the persisted checkpoint.

## Operations and limits

- `execute` (default): run `code` and preserve globals; save a checkpoint on success.
- `variables`: list visible top-level names tracked from successful assignments.
  This is a convenience list, not complete runtime introspection: it can omit
  names and become empty after a failed checkpoint commit.
- `checkpoint`: explicitly save the current session.
- `reset`: clear the live session and delete `kernel.bin` and `kernel.names.json`.
  It retains quarantine, temporary leftovers and the shared venv.

Requests are serialized because one Monty session is mutable. Monty has a
2-second feed-duration limit and a default 256 MiB sandbox memory limit
(`PI_RLM_MAX_MEMORY` overrides the byte count). These are not limits on total
pi process RSS or on external tools. The client has a 10-second response timer,
paused while a nested pi tool is running. Output and encoded result text are
capped at 4096 bytes each with a truncation indicator; error text is capped at
8192 bytes. No intentional high-memory test is required to use the package.

On the tested Linux target, the client starts each sidecar in a private process
session/group and observes only its known descendant edges through `/proc`
(including children created on other threads). Shutdown allows 2 seconds for
normal exit or TERM, then escalates the owned group and observed escaped members
to SIGKILL with a further 2-second deadline. Close and lazy restart wait for pipe
closure and observed PID/start identities to disappear; unresolved cleanup is
an error, not a successful restart. Callable pi tools run on pi's side of the
bridge, outside this subprocess scope, and are not signaled by this cleanup.

Real stopped Monty workers are covered by Linux regressions for startup timeout,
startup abort, concurrent close and request timeout. This requires readable Linux
`/proc` and OS reaping of orphaned exits. Other platforms' direct-child fallback
is not a tested descendant-cleanup guarantee. Start-identity checks guard against
stale observations, but Node's PID/group signals are not atomic pidfd operations.
This is bounded cleanup of the supported uv/Python/Monty tree, not a general
supervisor for arbitrary daemonizing programs: unobserved descendants escaping
the private group and external SIGKILL of pi are not covered.

## State, privacy and compatibility

The state root defaults to `~/.pi/agent/rlm`. Set `PI_RLM_STATE_DIR` explicitly to
move it; changing `PI_CODING_AGENT_DIR` alone does not move kernel state. Files
are stored under `<state-root>/<session-key>/`. The key is SHA256 of the pi
session-file path, or of `cwd + NUL + sessionId` for a session without a file.
Use `/session` to obtain the session file when identifying a persisted state
directory; hash that exact path, not the transcript contents. Do not launch
another pi process against a session already in use. Concurrent writers to the
same kernel directory are not supported.

`kernel.bin` is the authoritative raw Monty dump. `kernel.names.json` is companion
convenience metadata. Successful checkpoints stage private files, replace names
first, and commit the raw dump last. If raw replacement fails after metadata
replacement, prior raw data remains but the names file is discarded: after
restart `variables` can be empty while prior values still exist. Do not use it
as a recovery/data-completeness oracle. Failed execution restores prior live
Python data; filesystem failures are reported, not silently treated as success.

Checkpoint contents can include complete source/tool data, secrets and private
results. Final dump/name files have mode 0600 in a mode 0700 session directory;
temporary checkpoint files are chmodded to 0600 before replacement. Quarantine
inherits the dump's mode. Files are not encrypted, and filesystem permissions
are not protection from the account owner, privileged processes, backups or
other extensions. Treat `.bin`, `.names.json`, `.tmp`, `.names.tmp` and `.corrupt`
files as sensitive. Do not commit, share or log them. The sandbox's limits do not
restrict the authority of pi tools you allow it to call.

Restore is supported only with this reviewed package/dependency pin. There is no
cross-version dump migration guarantee, new checkpoint envelope, or automatic
upgrade strategy. Keep a private backup before a deliberate version change;
revert to the matching pin or reset if restore is incompatible.

If Monty cannot load a dump, the sidecar attempts to rename it to `kernel.corrupt`
and starts fresh empty Python state with an empty tracked-name set. This loses
accessible variables/data from the live kernel; it is not a successful recovery
of the damaged contents. Quarantine preserves bytes for private diagnosis, not
an automatic repair. A later quarantine can replace an earlier one; stop and
back it up privately before retrying. Failure to read/rename state can instead
fail startup. Do not rely on a startup warning or the names file to detect loss.

For recovery, stop the owning session, retain a private copy of the affected
files if wanted, fix access/dependency issues, and restore a known-good dump from
the matching pin or deliberately start fresh. Do not blindly rename corrupt
bytes back to `kernel.bin` or expect a different Monty version to repair them.

## Pi session behavior

- Resume and runtime reload retain session-file identity and restore its checkpoint.
- `/new`, `/fork`, and `/clone` create another session file and therefore a fresh,
  isolated kernel, even if they copy transcript text. They do not copy Python data.
- `/tree` stays in the same session file with linear mutable Python state.
  Navigating to an older transcript branch does not rewind variables.
- Different session files use different checkpoint directories. Moving a session
  file changes the key; ephemeral sessions cannot be resumed after process exit.
- Transcript compaction does not clear this external checkpoint store.

Resume/reload and new-session behavior have runtime evidence; tree/fork/clone
semantics follow pi's documented session lifecycle and the current identity
implementation. Dedicated broader lifecycle verification remains a separate gate.

## Installation

The selected durable source is an ordinary, independent git clone at
`~/.local/share/pi-extensions/rlm-kernel-v1`, checked out at a reviewed commit.
Preparing this source does not install it into personal settings. The initial
runtime preparation is pinned to reviewed `d61eb4fad3bc4f8579b85467c0ba7abca1085a69`;
after the remaining lifecycle/live/RSS gates, repin the clean clone to the final
reviewed production HEAD. Do not describe the initial pin as completed v1.
No publication is needed. When preparing it intentionally, substitute the
reviewed full commit ID and an existing local repository path:

```bash
mkdir -p "$HOME/.local/share/pi-extensions"
git clone --no-hardlinks /absolute/path/to/local/pi-extensions "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
git -C "$HOME/.local/share/pi-extensions/rlm-kernel-v1" checkout --detach <reviewed-full-commit-id>
git -C "$HOME/.local/share/pi-extensions/rlm-kernel-v1" rev-parse HEAD
```

Only after separate personal-install approval:

```bash
pi install "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
pi list
```

Use a previously nonexistent destination and verify the commit before installing.
The preparation above ends at `rev-parse`: run `pi install`/`pi list` only after
separate approval for personal installation/migration. If the destination already
exists, classify its provenance and clean status before repinning; never overwrite
unrelated data. `--no-hardlinks` copies local Git objects; confirm there is no
`.git/objects/info/alternates`. Repinning after final review also requires a clean
checkout. If the final commit is not already present, fetch it from the authorized
local repository path (not a remote service): `git -C <durable-clone> fetch
/absolute/path/to/local/pi-extensions <final-reviewed-full-commit-id>`. Then run
`git -C <durable-clone> checkout --detach <final-reviewed-full-commit-id>` and
`git -C <durable-clone> rev-parse HEAD`.
A local pi package is loaded in place, not copied; keep this clone intact and
unchanged. The clone is not a worktree or an alternate-object link to disposable
storage. Pi 1.1.0 rejected direct `git:file://...` package installation; do not use
that spelling. Remote/npm publication and existing personal-install migration
require separate approval. The root manifest loads every extension in this repo;
use pi's package resource filters to narrow the loadout if desired.

After an intentional installation, restart pi or `/reload` before expecting the
new loadout. Only a no-model isolated installed-readiness check proves bootstrap;
package listing/tool registration alone is insufficient.

To remove that package settings entry:

```bash
pi remove "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
```

Restart/reload or exit running sessions to release their sidecars. Removal leaves
the local clone and sensitive kernel state. Deleting a transcript or clone does
not erase its kernel files either.

## Explicit cleanup (destructive user action)

Stop all pi sessions using the affected state root before cleanup. Never delete
state under a running sidecar. Determine the actual root (including any override)
and the exact session key, and verify the directory. The following is a template,
not an automatic uninstall hook; replace the placeholder with one confirmed
session directory, not the root, home directory or an arbitrary glob:

```bash
session_dir="/absolute/path/to/rlm/<confirmed-session-key>"
ls -ld -- "$session_dir"
# Deletes Python user data, metadata and quarantine for this one session:
rm -i -- "$session_dir/kernel.bin" "$session_dir/kernel.names.json" \
  "$session_dir/kernel.tmp" "$session_dir/kernel.names.tmp" "$session_dir/kernel.corrupt"
rmdir -- "$session_dir"
```

Review each prompt. Missing-file errors are harmless; do not force removal of
unexpected contents or directory obstacles. `rmdir` succeeds only when empty.
Reset removes only dump/names, so explicitly remove quarantine and leftover temp
files if sensitive-data deletion is intended. To delete all sessions' kernel
data, repeat the verified per-session procedure for each session directory;
do not mistake uninstall or deletion of `.venv` for deletion of user data.
Normal deletion is not secure erasure of backups or storage snapshots.

The shared `.venv` is disposable dependencies, not the checkpoint store. To
repair/reclaim it after stopping all sessions, verify the exact `.venv` path and
remove only that directory (for example, `rm -rI -- /absolute/path/to/rlm/.venv`).
The next bootstrap recreates it from the lock, potentially needing downloads.
This leaves user data/quarantine in session directories. Conversely, deleting
session directories leaves the venv. Retained private backups need their own
explicit deletion decision.

## Testing and scope

Focused verification commands from the repository root:

```bash
HINDSIGHT_DISABLED=true timeout 60s python3 packages/rlm-kernel/install-check.py
HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/integration.py --case installed
HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/integration.py --case installed --installed-root /absolute/path/to/independent/pi-extensions
timeout 20s python3 -B packages/rlm-kernel/python/recovery_budget_tests.py
PYTHONDONTWRITEBYTECODE=1 timeout 90s uv run --project packages/rlm-kernel/python --locked python packages/rlm-kernel/python/check.py
timeout 90s python3 packages/rlm-kernel/isolated-check.py --timeout 85 -- uv run --project packages/rlm-kernel/python --locked python packages/rlm-kernel/python/check.py
timeout 30s node packages/rlm-kernel/check.mjs
timeout 15s node packages/rlm-kernel/bridge-check.mjs
```

`install-check.py` is an explicit distribution check, not an added default npm
command. It clones the current committed HEAD twice with copied Git objects,
detaches the independent installed checkout, verifies all six runtime/lock files
and the manifest against committed blobs, and renames only its owned original
fixture. Dirty/uncommitted runtime or original-file aliases fail. Actual pi
install/list creates the private settings declaration; only then does an RLM-only
resource filter narrow it. The same qwy readiness oracle proves automatic
package discovery plus checkpoint/variables, 42/43 and fresh restore, and rejects
real missing-uv and failed-bootstrap fixtures. Actual remove/list and a fresh
no-model host then prove `python` absent and no observed owned child spawned.
The real private checkpoint/names and planted quarantine/venv markers retain
identical bytes/modes until deliberate owned-fixture cleanup. Every CLI command
has an 8-second supervisor deadline, the worker has 50 seconds plus bounded
cleanup, and the outer command has 60 seconds. No personal settings, paid model,
real checkpoints or actual worktree rename is involved. A retained clone/state
is not an uninstall failure; explicit cleanup is a separate destructive choice.

For standalone packaging evidence, run `timeout 40s python3
packages/rlm-kernel/package-check.py`: its artifact step invokes actual `npm pack
--dry-run --json --ignore-scripts` with private npm config/cache and asserts
required sidecar/project/lock/probe/check files, no generated venv/cache/state,
unbundled host peers, and the unchanged strict default-test argv contract.

The installed selector uses private package settings and RLM-only resource filters,
not `--extension` pointing at the original index. Without `--installed-root`, it
copies the manifest and six runtime/lock files into an independent owned fixture.
With the selector, it discovers the given independent package root in private
settings; this is a reusable readiness oracle, not proof of install/list/remove
(the separate distribution gate owns those CLI operations). It rejects original
paths, symlink escapes and original-file aliases. The host-loaded probe imports
that independent factory with a different session-file identity from the
automatically loaded instance, explicitly closes it, and checks actual
checkpoint/variables replies, 42/43 and a fresh-generation restore of 43. Evidence
includes source path/inode, PID/start/parent identities and installed sidecar argv.
Missing uv and a deliberately failing bootstrap process must reject checkpoint
readiness with a diagnostic within 8 seconds; registration alone cannot pass.
All cases assert no owned survivors before supervisor backup. Default integration
still runs the 24 callback groups, five protocol checks and EOF/TERM/KILL, then
these three installed cases. No model or personal settings are used.

Use private temporary state/venvs for direct checks (set `UV_PROJECT_ENVIRONMENT`
and `PI_RLM_STATE_DIR`), not an active session's files. Real-pi checks must use
`isolated-check.py` for private agent/cwd/session/state setup and owned cleanup.
If locked dependencies need prewarming, bound that bootstrap separately; a
cache-only `UV_OFFLINE=1` run fails when required dependencies are unavailable.

Default recovery verification runs three independent owned groups: capture (8s),
checkpoint failures plus successful/empty-name restores (20s), and stalled workers
(8s). These are intentional aggregate resource caps, not a performance promise
under varying CPU availability or permission for every child to consume its full
5s deadline. The planted stalled-worker deadlines remain 0.7s. Each supervisor
has its existing separate cleanup allowance; the isolated narrow command uses
85s internally with a 90s outer TERM guard. Any group failure stops dispatch,
without retries or catch-and-continue success. Output reports four executed
methods, 13 failure scenarios, 28 fresh-process restores and bounded group/child
timings. Passing these commands does not identify the cause of the earlier
20s aggregate overrun.

The isolation runner self-test has 29 named independent logical scenarios:
14 isolation/environment/command cases, three outer-signal cases and 12
safety/after-exit cases. Each runs serially in a fresh supervised process with
private fixtures, synthetic poison configuration and an unrelated sentinel.
Original operation/readiness guards and signal/error precedence are unchanged.
`--list-cases` emits JSON names and per-case budgets without creating fixtures;
`--self-test-budget` returns an inert integer total for the selected cases.
Unknown/duplicate selectors and missing/duplicate/unknown registrations fail
before execution. Only 29 original unique successful executions with distinct
fixture/process identities qualify for full PASS; a selected subset is labeled
"not full-suite PASS". Failure stops dispatch and lists remaining unrun cases.

Run from the repository root, offline with the existing cached dependencies:

```bash
export PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true
budget=$(python3 packages/rlm-kernel/isolated-check.py --self-test-budget)
timeout "$((budget + 3))s" python3 packages/rlm-kernel/isolated-check.py --self-test
name=tree-exit7
budget=$(python3 packages/rlm-kernel/isolated-check.py --self-test-budget --case "$name")
timeout "$((budget + 3))s" python3 packages/rlm-kernel/isolated-check.py --case "$name"
timeout 20s python3 packages/rlm-kernel/self_test_controls.py
# Validate 29 actual CASE_RESULT records in a saved full self-test log:
timeout 20s python3 packages/rlm-kernel/self_test_controls.py --verify-log /path/to/self-test.log
timeout 10s python3 packages/rlm-kernel/isolated_check_tests.py packages/rlm-kernel/isolated-check.py --capture-only
timeout 10s python3 packages/rlm-kernel/isolated_check_tests.py packages/rlm-kernel/isolated-check.py --active-only
```

Case budgets cover the existing operation guard, 3s supervision cleanup, 1s
fixture setup and the original 2s sentinel teardown; ownership probes also
allow their original readiness/wait/teardown guards. The total is the sum of
selected case budgets plus 3s per-case supervision and 1s suite setup (currently
375s for all 29); the enclosing outer guard adds the existing 3s allowance.
This replaces the old aggregate 15s internal/20s outer self-test command, not a
performance promise or a change to any operation timeout. The recorded earlier
15.523s/exit124 failure remains historical evidence, not a runtime-fix claim.
Eight retained dispatch controls exercise fail-closed inventory/ledger handling,
actual selected cases, synthetic process exit7/timeout124/missing-receipt125,
and parent TERM cleanup without killing an unrelated caller child. Positive
all-29 independence evidence comes from the actual full execution log, not mocks.
Capture-only and active-only remain separate, including recovery's capture hook.

Use the [root isolated testing commands](../../README.md#test). The broader
full-suite guard is unchanged and must be evaluated at the completion gate.
The default suite covers sidecar, bridge, client, checkpoint-write failures and
real extension registration/cleanup without a paid provider. Structural package
checks do not prove narrative semantics. Default integration proves installed
readiness; the explicit distribution check also proves actual install/list/remove
and private retention/cleanup. Opt-in paid lifecycle checks remain a separate
completion gate.

## No-inference real-pi permission replay

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/permissions_replay.py
```

A focused stream-only regression is also available:

```bash
PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true timeout 20s python3 packages/rlm-kernel/permissions_replay.py --stream-test
```

Both commands first run 13 offline stream-contract checks through an isolated
Pi command loader using the actual host pi-ai helper. Normal tool/text events
must balance; pre-abort and cancellation inside awaited payload/start/content/
terminal instrumentation must emit one `error/aborted`, no `done`, and close.
Callback throws, missing tools and excess continuation must emit `error/error`.
This focused check captures only the provider config and calls its local stream;
it does not invoke a model, RLM callback or core tool hooks. It reuses the
required replay artifact and owned cleanup, with no SDK dependency or new file.

The full standalone proof uses installed pi 1.1.0's normal agent loop with a tiny
local deterministic provider stream, `q1s-local-replay/fixed-python`. It is
transport replay, NOT LLM inference or model-quality evidence. The test-only
literal availability key is not a credential, and `local://q1s-no-network` is
never contacted. No configured provider package, user model/auth copies, paid
requests or syscall tracing are used. All usage and cost fields must be zero;
exactly two assistant responses (`toolUse`, then `stop`) are required. The live
oracle's positive-usage check is unchanged and rejects these zero-usage messages.

The existing isolation/install helpers create private agent/cwd/session/state
fixtures, actually install an independent committed clone with copied objects,
filter discovery to RLM only, and make the owned original clone unavailable.
The replay loader does not import or replace the RLM factory or builtin read.
It emits the exact `permissions.CELL` as one provider tool call; pi core invokes
the actual installed Python callback with its real tool context, owns nested
read execution and IDs, and dispatches the real permission hooks. No execution
or hook events are manually emitted. The existing 36-check collector retains
all execution, argument, parent-ID, result-envelope, hook, privacy, cumulative
inode-sensor and cleanup checks; only the replay-specific zero-inference check
replaces positive model usage for this command.

Three real-pi cases run once: good (36/36 PASS), read-before-denial (expected
strict FAIL from actual discarded denied-file IO), and lost enforcement
(expected strict FAIL from the actual builtin denied read, privacy and result
checks). The reporter never prints the unique denied sentinel or arbitrary
arguments. Cumulative startup/before-prompt/after-settled/final-shutdown sensor
counts are never reset. Each case requires orderly EOF and zero owned survivors
BEFORE backup, then zero children after finally cleanup. Private source/config/
state roots must be absent afterward; an unrelated caller sentinel must remain
alive. The worker has a 36-second deadline inside the 45-second outer guard.
This is fixture isolation, not an OS security sandbox.

Passing replay does not attribute or fix the historical live denied-access
observation (9072, denied count 1), or the unexplained synthetic positive
failures. It does not complete ios.1 or replace the separately gated full
lifecycle/live/RSS or final durable-repin checks. Default npm test and
the strict default-test argv contract remain unchanged.

## Installed lifecycle smoke and release disposition

On 2026-10-23 the user directed "close576 notconcern/IUAnoblocker":
`pi-extensions-576` is closed as accepted risk, NOT fixed. `pi-extensions-iua`
is an open P3 non-blocking follow-up, with a relates-to edge only. The historical
live denied-access count 1 and unexplained synthetic positive failures remain
unattributed; neither is certified PASS. Strict permission oracles and historical
evidence remain unchanged. Core nested hook ownership has separate q1s replay
proof. Inode-attribution closure and another paid permission diagnostic are NOT
release prerequisites. Lifecycle/live/RSS and final reviewed durable repin remain
required; `4px` and `ios.1` are not complete from offline infrastructure alone.

From the repository root, without credential access or inference:

```bash
PI_RLM_TEST_MODEL= HINDSIGHT_DISABLED=true timeout 10s python3 packages/rlm-kernel/e2e.py
PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/lifecycle_controls.py
# Focus only the actual-host direct-read negative:
PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/lifecycle_controls.py --negative direct-read
/usr/bin/time -f 'V1_SUITE elapsed=%e max_rss_kb=%M exit=%x' env HINDSIGHT_DISABLED=true timeout 180s python3 packages/rlm-kernel/isolated-check.py --timeout 120 -- npm test
```

The default `e2e.py` case is `lifecycle` (`resume` remains an alias); absent model
selection reports SKIP before any helper import, config/auth copy, installation
or runtime bootstrap. Missing credentials after opt-in are failures, not skips or
success. The maintained controls prove early SKIP with unavailable config/runtime,
seven exact oracle outcomes (including wrong value, boolean false/zero, IDs,
code, settlement and zero-paid-usage rejection), five RPC framing/deadline cases,
and the actual installed real-Pi lifecycle through a fixed local stream. First,
the same transport issues a direct `read(path="large.txt")` in the seed phase
without a Python parent. The actual hook must reject it before builtin execution:
one rejected call, no tool-result hook, one error execution-end with the specific
guard reason and no execution duration, no returned fixture content, cumulative
IN_ACCESS zero at startup/before-prompt/settled/shutdown, and zero owned survivors
before backup. This is a separate owned host, not fabricated execution events.
Five retained-state phases also reject wrong-value expectations against their actual
RPC events; all six phases report verified hook/start/end counts and exact IDs.
This is local transport replay, NOT inference. Only the offline
loader intercepts compaction with a labeled synthetic summary; live never loads
it. No real model/auth source is read by controls.

Both modes use actual install/list of an independent committed clone, RLM-only
automatic discovery, private agent/cwd/session/state/venv and owned-process
supervision. Six bounded Python queries seed x=40 plus the real nested read's doc
and matches, advance to 42, then query unchanged retained data after runtime
reload, compaction and process resume. No later query rebuilds variables or
rereads data. A temporary `ctx.reload()` command must produce a different runtime
generation with the same session file. A manual compaction uses effective
keepRecentTokens=256/reserveTokens=4096, and its actual entry must match both RPC
and persisted session file. A bounded custom-message boundary avoids split-turn
two-summary compaction; it is fixture context, not generated model evidence.
Live requires a non-hook provider summary and positive persisted usage. EOF,
exit0 and zero owned survivors are required BEFORE launching a new process with
the same session file. A different new session must return NameError. Temporary
tool hooks guard exact code/count and correlate Python and nested-read IDs.
Only the accepted seed Python call's exact `<toolCallId>/1` may read `large.txt`,
using pi 1.1.0's documented nested-ID contract rather than assuming a parent
field on the hook. The accepted ID is cleared at Python result and every prompt;
a path/phase match alone cannot authorize a read. Unexpected calls cannot execute.
A guard aborts an excess assistant continuation.
No runtime limits, SDK dependency or default npm argv change is involved.

After review, and ONLY with explicit authorization for the exact live command
and selected source model/auth access:

```bash
/usr/bin/time -f 'V1_LIVE elapsed=%e max_rss_kb=%M exit=%x' env PI_RLM_TEST_MODEL=litellm/us.openai.gpt-6.1-sol HINDSIGHT_DISABLED=true timeout 180s python3 packages/rlm-kernel/e2e.py
```

Live reuses the normal configured LiteLLM provider and selected-only private auth
helper described below. Budget: six prompts with at most two assistant responses
each, one manual compaction (non-split fixture, one summary request), no retries.
Prompt settlement and compaction each have 30s deadlines; RPC setup 10s; orderly
process EOF 5s. The total live worker is capped at 150s plus supervisor cleanup,
inside the 180s outer guard. Offline worker is capped at 36s, with 12s phase
limits inside 45s. Reports allow only fixed outcomes, source commit/mode, sanitized
numeric usage/cost estimates and explicit accepted risks; raw state, model
headers, provider stderr and transcripts are never printed. Small-fixture
compaction is not a claim of context savings or performance. Actual live proof,
measured live RSS and final reviewed clone repin are still pending.

## Opt-in live permission check

The credential-free assembly regression is a separate narrow command:

```bash
PYTHONDONTWRITEBYTECODE=1 timeout 30s python3 packages/rlm-kernel/permissions_assembly_tests.py
```

This is synthetic guard proof, NOT actual pi live-policy proof. It runs actual
`live_check`, binary-pipe `LiveRpc`, Linux inode sensing and the Node hook logger
with a tiny synthetic host and owned sidecar marker. The host reads the allowed
fixture only after the actual hook permits it and never executes the blocked
read. Fixture events settle last; EOF gates final-shutdown faults. Positive,
late-denied, wrong-argument, provenance, exit, survivor, protocol, privacy and
startup-observation controls retain all 36 checks and four safe phase slots
before assertions (unavailable observations are null, not zero). Three private
in-memory helper mutants prove sensitivity to lost final conjunction, provenance
and late-denied guards; no real runtime sidecar or provider/config is loaded.
Owned supervision cleans fixture children even on failure, without signaling an
unrelated caller sentinel. Model/credential environment variables are not passed.
This file is required in the package artifact, not added to default npm argv.

The bounded old-order comparison also passes when, as in the copied review
probe, file IO finishes before any events are emitted. Settled-first was invalid
fixture chronology, but is NOT an established cause of the earlier positive
failures. Those failures and the earlier live denied-access observation remain
unresolved; these offline controls do not certify a runtime fix. The user
accepted the risk on 2026-10-23; it no longer blocks lifecycle/RSS or repin.

The maintained permission oracle is explicit and paid, not part of `npm test`:

```bash
PYTHONDONTWRITEBYTECODE=1 timeout 20s python3 packages/rlm-kernel/permissions_tests.py
HINDSIGHT_DISABLED=true timeout 90s python3 packages/rlm-kernel/e2e.py --case permissions
PI_RLM_TEST_MODEL=litellm/us.openai.gpt-6.1-sol HINDSIGHT_DISABLED=true timeout 90s python3 packages/rlm-kernel/e2e.py --case permissions
```

Without `PI_RLM_TEST_MODEL`, the command clearly reports SKIP. The live command
requires explicit authorization to read the selected agent's `models.json`,
`models-store.json`, and ONLY the `litellm` entry of `auth.json`. Do not run it
with credential access denied or treat model selection as credential approval.
The helper parses source auth in memory without inspecting other entry values,
serializes only `litellm` into an exclusively created 0600 copy under a private
0700 agent, and never prints/hashes credential values. Source auth is read-only;
in-memory byte equality is checked after the run. Normal SDK refresh may change
only the private copy. Auth and model copies are deleted in `finally`, including
setup/body failures. Synthetic two-provider controls test selection, private
modes, existing-path rejection, source equality and failure cleanup without real
credentials. Existing source `pi-provider-litellm` 4.4.1 is loaded read-only
through normal package discovery; no network installation, new provider or
credential guessing is involved. Use only the authorized configured selector.

The worker actually installs an independent committed checkout with copied Git
objects and no alternates/origin, checks manifest/runtime provenance, and makes
only its original fixture unavailable. Agent, cwd, sessions, state and venv stay
private; user settings, checkpoints and worktrees are not moved. One exact Python
cell calls the builtin allowed and denied reads through pi's owned nested
execution. A temporary real `tool_call` hook blocks the denied read with
`IUA_DENIED`; it never replaces read or fabricates events. The oracle asserts
actual cell/result details, exact nested arguments, call IDs and parent
correlation, hook/result counts, error status and positive selected-model usage.
Linux inode `IN_ACCESS` independently requires actual allowed access and zero
denied access, distinguishing denial from post-execution redaction. A random
denied sentinel unknown to the model is checked against RPC, stderr and the hook
log without exposing its value. RPC and hook JSON are checked recursively after
decoding, including keys and fields that sanitization discards, so JSON escapes
cannot hide a leak. RPC sensitive fields and provider diagnostics are suppressed.
The bounded reporter collects named checks independently even when the sensor
fails. It records cumulative startup, before-prompt, after-settled and final
shutdown counts; any denied access or decreasing counter fails. No counter is
reset and no phase is exempt. Actual Python/nested argument matches, real hook
names/IDs/decisions/results, envelope/status, usage, privacy and protocol outcomes
remain visible without raw code, uncontrolled paths, error strings or provider
payloads. The hook log stores only fixture-path whitelist values, match flags,
valid-format IDs and fixed denial reasons. Rejected leaked/malformed RPC records
are discarded before storage; later available records can still be inspected,
but the privacy/protocol failure is permanent. Every check must pass; a safe
failure report is printed before a constant invariant assertion. Diagnostics do
not repair runtime behavior or explain earlier intermittent sensor failures.
Orderly EOF must leave zero owned survivors BEFORE supervisor
backup; all private fixture roots must be absent afterward. This narrow check
proves nested permission denial, not the separate full lifecycle/RSS gate.

V1 does not load Python-backed skills, expose arbitrary host objects, provide
full CPython, support image results or parallel tool calls, implement delegation
or subagents, migrate user configuration, or add branch-state cloning.
