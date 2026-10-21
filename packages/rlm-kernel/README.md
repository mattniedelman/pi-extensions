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
`~/.local/share/pi-extensions/rlm-kernel-v1`, checked out at the reviewed completed
feature commit. Its preparation and installed-artifact verification are later
rollout gates, not a claim that this directory already exists. No publication is
needed. When preparing it intentionally, substitute the reviewed full commit ID
and an existing local repository path:

```bash
git clone --no-local /absolute/path/to/local/pi-extensions "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
git -C "$HOME/.local/share/pi-extensions/rlm-kernel-v1" checkout --detach <reviewed-full-commit-id>
git -C "$HOME/.local/share/pi-extensions/rlm-kernel-v1" rev-parse HEAD
pi install "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
pi list
```

Use a previously nonexistent destination and verify the commit before installing.
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
HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/integration.py --case installed
HINDSIGHT_DISABLED=true timeout 45s python3 packages/rlm-kernel/integration.py --case installed --installed-root /absolute/path/to/independent/pi-extensions
timeout 20s python3 -B packages/rlm-kernel/python/recovery_budget_tests.py
PYTHONDONTWRITEBYTECODE=1 timeout 90s uv run --project packages/rlm-kernel/python --locked python packages/rlm-kernel/python/check.py
timeout 90s python3 packages/rlm-kernel/isolated-check.py --timeout 85 -- uv run --project packages/rlm-kernel/python --locked python packages/rlm-kernel/python/check.py
timeout 30s node packages/rlm-kernel/check.mjs
timeout 15s node packages/rlm-kernel/bridge-check.mjs
```

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

Use the [root isolated testing commands](../../README.md#test). The broader
full-suite guard is unchanged and must be evaluated at the completion gate.
 The default suite
covers sidecar, bridge, client, checkpoint-write failures and real extension
registration/cleanup without a paid provider. Structural package checks do not
prove narrative semantics. Isolated installed-package readiness/removal and
opt-in event-based paid lifecycle checks are separate rollout gates.

V1 does not load Python-backed skills, expose arbitrary host objects, provide
full CPython, support image results or parallel tool calls, implement delegation
or subagents, migrate user configuration, or add branch-state cloning.
