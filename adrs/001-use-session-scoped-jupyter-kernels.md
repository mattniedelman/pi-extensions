# ADR-001: Use direct session-scoped Jupyter kernels

## Status

Accepted and implemented. The user's explicit decision to drop all sandboxing on
2026-10-27 supersedes the nono/systemd/container launcher, policy, cgroup, and
containment-qualification portions of this ADR and task plan. Commit creation,
personal-install migration, downloads, and publication remain separately
authorized.

Acceptance criteria and current tasks live in `bd show pi-extensions-ios.15`.
Historical task notes and failed qualification evidence remain records, not
requirements for the new direct-kernel design.

## Context

The user wants normal Python imports and automatic retention of useful
intermediate objects without explicit save/load bookkeeping. Monty provides
snapshots and rollback but cannot load ordinary Python packages. The user accepts
live-session state only and notebook semantics after errors.

The attempted sandbox qualification grew into substantial launcher, policy,
ownership and test scaffolding without delivering the live Jupyter workspace.
The user judged that effort disproportionate and explicitly removed sandboxing
from scope. Its historical failures and inconclusive diagnostics remain intact;
they do not need to be fixed to ship this design.

## Decision

Run one ordinary ipykernel per live pi session directly through jupyter_client.
Use pi as the interface, with no notebook web server, external kernel daemon,
reattachment service or sandbox launcher.

### State and execution

Cells are sequential. Normal CPython objects, functions and imported packages
remain in the namespace across calls and conversation compaction. Exceptions
retain assignments, mutations and external effects already completed. Do not
serialize state, replay cells or restore a workspace after process exit.

Keep execute, variables and reset. Variable inventory is bounded and does not
invoke arbitrary object repr. Checkpoint requests return an explicit unsupported
error. Reset, new session, transcript fork/resume after exit and extension reload
start fresh state with a notice. Kernel crash or forced restart reports state
loss. Existing Monty checkpoint files are not read, changed, migrated or deleted.

### Direct process and transport

Let the maintained Jupyter client launch, manage, interrupt and shut down the
actual kernel. Use its normal loopback transport and private connection file,
not custom strict Unix mediation, systemd units, cgroups or a service watcher.
Private connection files prevent accidental disclosure; they are not an OS
containment boundary. No pacing workaround is required to accommodate nono.

Python runs with the user's ordinary filesystem, network and process authority.
It can directly read files and access the network without calling pi tools.
Do not describe this as sandboxed, read-only, credential-isolated or confined.
Do not deliberately forward provider credentials merely to run Python, but there
is no claim that same-user Python cannot access them through the filesystem.

### pi tools and results

Retain the existing pi tool catalog and ctx.executeTool dispatch, including
validation, hooks, abort, accounting and nested-call identifiers. Each cell's
pi proxy is bound to its immutable generation/execution context and expires on
completion, cancellation or retirement. Stale calls cannot borrow a later cell's
context. This is invocation ownership, not hostile-thread isolation or a way to
restrict Python's direct host access.

Complete a cell only after its matching shell reply and IOPub idle boundary.
Associate output with parent request IDs; unrelated late output must not become
a later cell's result. Disable interactive stdin. Bound input/messages, collected
stdout/stderr, expression text and tracebacks. Initial MIME support is text with
explicit unsupported outcomes for richer results, never execution of HTML.
Use normal Jupyter subprocess-output behavior; no network-blocking workaround.

### Cancellation and cleanup

Keep operation deadlines, output limits and sequential concurrency. Interrupt
the actual kernel on cancellation. If it returns to idle within the interrupt
window, keep its workspace; otherwise shut it down and report state loss.
Use graceful Jupyter shutdown followed by bounded forced termination when needed.
Keep ordinary positive process ownership and idempotent cleanup; never signal
unrelated processes. Do not rebuild a containment framework to police arbitrary
Python-created processes. There are no enforced CPU, memory or task-count caps.

### Packaging and verification

Pin ipykernel/jupyter_client and locked dependencies, report bootstrap/cache
failures explicitly, and preserve the renderer and other packages. Preparation
and downloads remain separately gated; use already prepared dependencies offline
for verification when applicable. No automatic skill installer or host-object
framework is part of this change.

Verify retained imports/objects, partial mutation on error, correlated output,
cell-context expiry, reset/state-loss notices, cancellation and normal shutdown.
Use bounded tests and separate spec/quality reviews, not sandbox denial probes,
Landlock/seccomp traces, resource attacks or a new qualification harness.
The completion suite's peak per-process RSS ceiling remains 1048576 KiB; it is a
verification budget, not a runtime memory limit or aggregate memory measurement.
No new paid model run is authorized.

## Alternatives considered

- Keep Monty: does not meet normal Python package/import needs.
- Write a custom CPython execution loop: duplicates maintained Jupyter behavior.
- Keep nono/systemd/container sandboxing: explicitly dropped because its
  qualification/deployment complexity outweighed the requested feature.
- Persist arbitrary objects across exit or keep a reload daemon: not required.
- Use mutable current-cell tool dispatch: allows stale requests to borrow a
  later invocation and is still rejected.

## Consequences

The design becomes a normal persistent Python workspace rather than a sandbox
project. Direct execution means Python has the same practical host access as a
local user-run Python process. Tool hooks do not intercept direct Python IO.
State continuity is automatic while the process lives, not durable across exit.

The uncommitted sandbox test artifacts are abandoned implementation work, not
runtime dependencies or shipping requirements. Preserve historical evidence;
do not relabel prior failures as fixes or reuse their certificates.
The original persistence-guidance worktree, user-owned configuration and personal
installation remain untouched until separately authorized rollout.

## What would change this decision

A future requirement to execute untrusted code would be a new explicit security
and deployment decision. It must not silently revive this abandoned sandbox
framework. A need for durable state, richer output or automatic installs likewise
requires a separate scope decision.
