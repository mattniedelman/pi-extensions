# Preserve the pi-owned Monty runtime for v1

- Status: Accepted; user approved the revised design and task graph
- Date: 2026-10-19
- Scope: `pi-extensions-ios.1`
- Acceptance criteria and executable task plans: `bd show pi-extensions-ios.1`

## Context

`packages/rlm-kernel` already provides persistent Python-subset execution with
Monty 1.1.0, a bidirectional JSONL tool bridge, and checkpoints outside the pi
transcript. The kernel, natural-tool bridge, and local installation were proved
by `z4b`, `ero`, and `8rt`. The later bounded dogfood tasks `ios.13` and `ios.14`
proved retained source/log data, runtime reload, provider-backed compaction,
process exit/resume, and session isolation. Their temporary harnesses are useful
evidence, not a maintained test suite.

The original development sources, root test registration and ignore rules were
captured at `26d404c`; no baseline recapture or runtime rewrite is needed. The
user's personal development installation is intentionally unchanged. The v1 gap
is reliable packaging, repeatable boundary tests, explicit support/recovery
documentation, and a durable installation source -- not another execution
architecture.

## Decision

### Runtime and capability ownership

Retain one extension-owned Python sidecar per pi session, with Monty 1.1.0 pinned
by `python/pyproject.toml` and `uv.lock`. Sidecar lifetime follows `session_start`
and idempotent `session_shutdown`; dependency bootstrap starts at `session_start`,
not first tool use. Keep the `python` tool model-only and retain its existing
`execute`, `variables`, `reset`, and `checkpoint` operations.

All external capabilities resolve against the current callable pi tool context
and execute through `ctx.executeTool`. Python-side `pi.call` and natural methods
are adapters, not another permission, credential, tool-validation, or usage
system. Preserve the JSON-compatible `content`, `structured`, `is_error`
envelope, explicit unsupported-result/size errors, and recursion guards.

Keep execution requests sequential: one Monty session is mutable. Add only
hardening changes required by failing verification, such as bounded startup,
abort, teardown, and transport-failure handling. Do not add delegation, Python
skills, parallel calls, images, rich-object proxies, or pi-core changes.

### State and transcript ownership

Keep state identity derived from the pi session file, with the existing
working-directory/session-ID fallback for ephemeral sessions. Successful
execution checkpoints the Monty state outside the transcript. Python data
rolls back after failed execution; completed pi-tool side effects do not. The raw
dump commits last, after staging/replacing convenience-name metadata. If raw
replacement fails, prior persisted data remains, but the names file may be
discarded; a fresh kernel can have an empty `variables` list with prior data
still available. Names are not complete introspection or a data-loss oracle.

Keep `/tree` linear with respect to mutable kernel state: transcript navigation
does not rewind variables. A different session file, including `/new`, `/fork`,
or `/clone`, gets isolated kernel state. Resume and runtime reload retain the
same identity. Do not silently clone or delete checkpoints on branch changes.

Retain the existing raw Monty dump and companion convenience-name metadata.
Treat the pin as the compatibility boundary; cross-version restore is not a
promised migration feature. Document corruption quarantine, failed bootstrap,
reset behavior, retained quarantine/venv files, and private but unencrypted
checkpoint contents. Any failure discovered during hardening must have an
explicit bounded outcome, not an invented recovery guarantee.

### Distribution without publishing or user-config migration

Use an independent ordinary git checkout pinned to the completed feature commit
as the local v1 installation source. Its intended durable location is
`~/.local/share/pi-extensions/rlm-kernel-v1`, outside disposable worktree storage.
Clone from the existing local repository; do not modify its main checkout or
push/publish anything. Pi can install the checkout as an ordinary local package.

Keep the user's current development installation unchanged unless separately
authorized to migrate it. Isolated install/remove and live smoke verification
must use temporary agent configuration and state directories. A durable checkout
can be prepared without replacing the user's active development loadout.

An isolated feasibility probe verified that a commit-pinned independent checkout
still loads through `pi install` after the original source directory is removed.
Pi 1.1.0 did **not** accept a `git:file://...` package source, so that spelling is
not a supported installation recipe. Remote commit-pinned git or npm sources can
replace the local checkout after separately approved publication.

### Verification boundary

Retain working sidecar/client/bridge checks. Isolate the initial baseline before
running it: every real-pi fixture owns its agent configuration, working directory,
sessions, state, and process group. Cleanup must be bounded on success, EOF,
assertion failure, and timeout; disabling Hindsight alone is not isolation.

Add a lightweight injected extension
harness for the registered callback and lifecycle/identity behavior, and a bounded
real-pi no-model installation/discovery/cleanup test. Do not add a pi SDK runtime
dependency merely to test the extension. Injected wiring tests do not establish
actual pi permission hooks; live event-based tests cover that boundary.

Keep paid provider tests opt-in. They must assert actual tool calls/results,
observe lifecycle/compaction entries, isolate session identity, await settled
runs, impose wall deadlines, and clean up all owned descendants. Record measured
peak RSS for the full suite and the installed-package live smoke. Do not confuse
whole-tool duration with checkpoint-write latency or a short compaction probe
with demonstrated context reduction.

## Alternatives considered

| Axis | Alternative | Why not selected for v1 |
| --- | --- | --- |
| Execution and state ownership | Move execution into pi core or replace Monty with full CPython | Reopens already-proved boundaries, broadens authority, and exceeds the bead's scope. |
| Capability data flow | Direct sidecar access to host objects, credentials, or a separate HTTP tool registry | Duplicates pi policy and weakens the tested `ctx.executeTool` boundary. |
| Transcript state ownership | Per-leaf checkpoints or implicit fork/clone state copies | Changes public behavior and storage costs; the evidence-gated branch decision remains `ios.5`. |
| Distribution ownership | Continue installing the editable worktree | Useful for development but does not satisfy a source independent of disposable worktree lifetime. |
| Distribution ownership | Immediately publish npm or push a remote ref | Requires user publication approval and infrastructure not needed for a maintainable local v1. |
| Distribution protocol | Install a commit-pinned `git:file://` source directly | Rejected by the actual pi 1.1.0 installer in a bounded isolated probe. |
| Verification implementation | Import/install the whole pi SDK for unit tests | Adds a dependency and coupling where an injected host harness plus actual CLI checks suffice. |
| Verification scheduling | Run paid model lifecycle tests in every `npm test` | Makes the default suite credential/network-dependent and needlessly expensive. |

## Consequences

The kernel remains a small ordinary extension with an explicitly limited Python
runtime. Hardening can ship without committing to the future delegation API.
The durable local checkout is a maintained source directory, not a registry
release; switching to a published source is a later operational choice.

Checkpoint data remains sensitive and unencrypted. Kernel snapshots and name
metadata are not a new general transactional database. Documentation and tests
must distinguish guaranteed data behavior, convenience metadata, and unverified
platform/version support. Initially claim only the tested Linux/pi 1.1.0 path;
other platforms or later pi APIs require evidence rather than blanket assertions.

## Independent design review

The initial review found no Critical architecture flaw, but identified an actual
checkpoint-write durability defect and Important verification gaps. At that
review, a bounded file-failure probe showed that name-metadata failure after
replacing the raw dump rolled back live memory while a fresh kernel restored the
failed assignment. Task `pi-extensions-6th` fixed the ordering (metadata first, raw
dump last), with failing-then-passing persisted-write/replace and fresh-process
checks. The raw format is unchanged and the convenience-name limitation above is
intentional. Independent spec and quality reviews approved the recovery work at
`a167c4e`. The raw-format change trigger below remains in force.

The revised task graph puts isolated baseline execution first, explicitly tests
checkpoint-write failures, and distinguishes installed registration from actual
sidecar readiness. Live policy verification uses a controlled allow/deny hook
with observable arguments and denial outcomes rather than successful nested
execution events alone. These revisions remain inside the bead's existing scope.
An independent review passed the revised design/task graph, and the user
approved it for autonomous local implementation and commits. Task
`pi-extensions-vmk`'s isolated runner and `pi-extensions-6th`'s recovery work have
completed independent spec and quality gates. This acceptance records those
approvals, not completion of the remaining extension-boundary, distribution,
installed-readiness, live-policy or final-suite tasks. Durable clone preparation
is deferred to `pi-extensions-zog`; personal migration and publication remain
unapproved.

## What would change this decision

- A bounded failure test proves the existing checkpoint format cannot support
  the required recovery behavior: pause for an explicit compatibility/migration
  decision instead of silently changing the format.
- A supported pi release changes callable-tool or lifecycle APIs: add a tested
  compatibility path or narrow support, rather than bypassing pi policy.
- Real dogfood demonstrates mistakes caused by linear `/tree` state: evaluate
  the alternatives in `ios.5`, not an incidental behavior change in this bead.
- The user approves remote publication or personal-install migration: replace
  the distribution source while preserving the runtime and session identity.
- Measured workloads require semantic children, Python packages, or concurrency:
  follow the separate delegation/skills/parallelism beads, not expand v1 scope.
