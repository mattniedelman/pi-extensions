# RLM Python workspace

The root [pi-extensions README](../../README.md) describes package installation
and removal. This page covers the RLM runtime itself.

The `rlm` extension provides Pi's model-only `python` tool as a direct,
session-scoped CPython workspace. It runs one ordinary ipykernel managed through
`jupyter_client`; it does not run a notebook web server.

## Security boundary

This runtime is not sandboxed. Python has the same practical filesystem,
network, and process authority as a local process running as the user. Direct
Python IO bypasses Pi tool hooks and permissions. The extension does not
intentionally forward arbitrary provider credentials or hook variables in the
kernel environment, but it does not provide credential isolation from files the
user can read.

The `pi.*` bridge is a separate invocation-ownership boundary. Each cell gets an
immutable generation/cell context for nested Pi tool calls. That context expires
when the cell completes, fails, is cancelled, is reset, or the session shuts
down. Saved proxies and late results cannot borrow a later cell's context.
Generation IDs are bookkeeping, not protection against hostile code running in
the shared Python process.

## Requirements and bootstrap

- Node.js 22 or newer
- CPython 3.13
- `uv` on `PATH`
- Linux for the full process-ownership and inotify verification suite

The locked Python project is `packages/rlm/python` and pins:

- `ipykernel==6.30.1`
- `jupyter-client==8.6.3`
- `pyzmq==27.2.0`

Production uses `PI_RLM_ENV_DIR` when set. Otherwise its dependency environment
is `${XDG_CACHE_HOME:-$HOME/.cache}/pi/rlm/python-3.13`. This environment holds
only dependencies; it is not a workspace checkpoint. `UV_OFFLINE=1` is preserved
only when explicitly set. An offline cache miss is an error and never retries
online automatically.

The extension starts no process from its factory. It launches the sidecar and
kernel on `session_start` and closes them idempotently on `session_shutdown`.
Bootstrap errors distinguish a missing `uv`, a failed child process, missing
Jupyter dependencies, and kernel readiness failure.

## Operations

### `execute`

Runs normal CPython code. Imports, objects, functions, classes, and mutations
remain available to later cells while the kernel is alive. Execution waits for
the matching Jupyter shell reply and matching IOPub idle boundary. Stdin is
disabled.

Notebook error semantics apply: an exception stops the cell, but assignments,
mutations, output, and external effects completed before the exception remain.
There is no rollback.

Text stdout, stderr, expression results, and tracebacks are byte-bounded while
messages continue draining through idle. Rich MIME output is currently rejected
explicitly rather than rendered or executed.

### `variables`

Returns a sorted, bounded inventory of visible top-level names without reading or
representing their values. It excludes underscore-prefixed internal names and
`pi`, returns at most 256 names, and caps the compact UTF-8 envelope at 16384
bytes. The result includes `truncated` when either limit applies.

### `reset`

Retires the current kernel and starts a fresh generation. It returns:

```json
{"reset":true,"state_lost":true,"notice":"Python workspace reset; previous live state was discarded"}
```

### `checkpoint`

Unsupported. The tool returns:

```text
Python checkpoints are unsupported; state lasts only for the live pi session.
```

There is no serialization, dump, replay, reconnect daemon, or restoration after
process exit.

## Session and failure behavior

Conversation compaction does not restart the live kernel, so in-memory Python
objects remain available. A new session, fork, resume into a new process,
extension reload, reset, sidecar crash, or process exit creates fresh Python
state. The first operation after a state-losing replacement reports once:

```text
Python workspace started fresh; state from an earlier process is not restored
```

After a normal interrupt reaches Jupyter idle, the same kernel generation and
namespace remain usable. If an interrupted kernel does not become idle within
the bounded window, the extension uses Jupyter's maintained shutdown path,
rotates the generation, and reports:

```text
Python workspace was lost after the interrupted kernel did not become idle
```

If cleanup itself fails, the client becomes terminal and requires an explicit
session lifecycle restart rather than silently creating competing authority.

Old Monty `kernel.bin`, names, temporary, and corrupt files are not loaded,
changed, migrated, or deleted. The production launch does not construct or pass
a legacy state path. The test suite uses only private synthetic legacy fixtures;
it never reads a user's old workspace files.

## Pi tool bridge

Python can call tools available in the originating Pi execution context:

```python
pi.read(path="README.md")
pi.call("read", {"path": "README.md"})
pi.tools()
pi.search("query")
pi.describe("read")
```

Natural methods are generated only for unambiguous valid Python identifiers.
`python` and `codemode` are blocked recursively. Nested calls retain Pi's
argument validation, hooks, abort signal, parent/nested IDs, result handling, and
usage accounting.

## Verification

Focused checks from the repository root use the locked test dependency group and do
not require model inference:

```bash
UV_OFFLINE=1 UV_CACHE_DIR=/tmp/pi-jupyter-qualification-cache \
  uv lock --project packages/rlm/python --check

UV_OFFLINE=1 UV_PROJECT_ENVIRONMENT=/tmp/pi-jupyter-qualification-venv \
  UV_CACHE_DIR=/tmp/pi-jupyter-qualification-cache \
  uv sync --project packages/rlm/python --group test --locked

UV_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 timeout 120s \
  uv run --project packages/rlm/python --group test --locked \
  python -W error::ResourceWarning -W error::DeprecationWarning -m unittest -v \
  packages/rlm/python/bootstrap_tests.py \
  packages/rlm/python/jupyter_sidecar_tests.py \
  packages/rlm/python/cancellation_tests.py \
  packages/rlm/python/operations_tests.py

UV_OFFLINE=1 UV_CACHE_DIR=/tmp/pi-jupyter-qualification-cache \
  timeout 120s node packages/rlm/check.mjs

UV_OFFLINE=1 UV_CACHE_DIR=/tmp/pi-jupyter-qualification-cache \
  HINDSIGHT_DISABLED=true PYTHONDONTWRITEBYTECODE=1 \
  timeout 120s python3 packages/rlm/integration.py

PYTHONDONTWRITEBYTECODE=1 timeout 20s \
  python3 packages/rlm/legacy_state_sensor_tests.py
```

`npm test` runs the registered package checks, direct sidecar/client/bridge
checks, and zero-inference integration suite. The legacy-state access oracle is a
separate Linux-only command because it runs a deliberate installed-source mutant:

```bash
env PYTHONDONTWRITEBYTECODE=1 HINDSIGHT_DISABLED=true UV_OFFLINE=1 \
  UV_CACHE_DIR=/tmp/pi-jupyter-qualification-cache \
  timeout 235s uv run --project packages/rlm/python --group test --locked \
  python3 packages/rlm/integration.py --case legacy-state
```

The working tree can be checked without staging through the package check's
explicit working-tree source mode. Committed-source verification remains a
separate post-commit release gate.

No paid provider run or personal installation activation is part of these
commands.
