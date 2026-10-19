# rlm-kernel

A standalone pi extension that gives the model one persistent, sandboxed Python-subset kernel. It runs Monty 1.1.0 in a Python sidecar and exposes one `python` tool.

## Requirements

- pi 1.1.0 or later
- `uv` on `PATH`

On first use, `uv` installs the locked sidecar dependencies into `~/.pi/agent/rlm/.venv`. State is checkpointed after each successful execution under `~/.pi/agent/rlm/<session-key>/kernel.bin`. Set `PI_RLM_STATE_DIR` to move this directory.

Checkpoints can contain complete tool results, including sensitive data. They are stored locally with 0600 file permissions inside a 0700 session directory and are not encrypted.

A failed execution discards the live Monty session and restores its last good in-memory checkpoint. Resuming the same pi session restores the file checkpoint.

## Pi tools from Python

Callable pi tools appear as natural methods inside the sandbox:

```python
readme = pi.read(path="README.md")
status = pi.bash(command="git status")
```

Use `pi.call("exact-tool-name", {...})` when a tool name is not a valid unique Python method. Use `pi.tools()`, `pi.search(query)`, and `pi.describe(name)` for progressive discovery. Methods resolve against the current pi tool context and execute through `ctx.executeTool()`, so validation, permission hooks, aborts, usage accounting, and nested-call records remain owned by pi.

Every call returns `content`, `structured`, and `is_error`. Image results are not supported. Results larger than 1 MiB fail rather than being silently truncated before Python analyzes them. Calls to `python` and `codemode` are rejected to prevent recursive orchestration.

Tool failures return the same envelope with `is_error` set, so Python can inspect any structured error data. Transport failures still raise a Python `RuntimeError`.

Python state rolls back when an execution fails. Effects from completed pi tools do not roll back. For example, a successful `pi.write(...)` remains on disk even if later Python code raises an exception.

## Operations

- `execute`: run code and preserve globals.
- `variables`: list top-level names tracked from successful assignments. This is a convenience list, not runtime introspection.
- `checkpoint`: save the current session.
- `reset`: clear the live session and delete its checkpoint.

Output and error text are capped before they reach the model. Requests are serialized because one Monty session is mutable.

## Local installation

From this repository:

```bash
pi install /absolute/path/to/pi-extensions
pi list
```

Pi loads every extension declared by the package. Remove the local package with:

```bash
pi remove /absolute/path/to/pi-extensions
```

## Pi session behavior

Observed with pi 1.1.0 for resume and `/new`; `/tree`, `/fork`, and `/clone` behavior below follows pi's documented session lifecycle and stable session-file identity:

- Resume and reload keep the session ID and restore the same kernel checkpoint.
- `/new`, `/fork`, and `/clone` create a different session file and therefore an empty, isolated kernel.
- `/tree` keeps the same session file and live kernel. Moving to an older transcript branch does not rewind Python variables. This spike deliberately adds no hidden branch cloning behavior.
- Two sessions with different session files use different checkpoint directories.

## Scope

This spike does not load Python-backed skills, expose arbitrary host objects, provide full CPython, support image results or parallel tool calls, or implement subagents.
