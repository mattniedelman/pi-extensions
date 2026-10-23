# pi-extensions

Extensions for [pi](https://github.com/earendil-works/pi).

| Package | What it does |
|---|---|
| [bedrock-sso](packages/bedrock-sso/) | `bedrock-sso` provider: Claude on Amazon Bedrock with prompt caching that works for Claude 5, and automatic `aws sso login` when the SSO session expires. |
| [cache-status](packages/cache-status/) | Status line below the editor with prompt-cache hit rate, token counts, and cost. Turns red when a Claude model stops caching. |
| [beads-reminder](packages/beads-reminder/) | In repos that use bd, keeps the `beads-workflow` start ritual and capture gate in the system prompt every turn. |
| [rlm](packages/rlm/) | Persistent Monty Python-subset kernel with pi-owned nested tools and per-session checkpoints. |

## Install and remove

Use a reviewed, ordinary local clone at a fixed commit, not a disposable development
worktree. The selected durable v1 source is
`~/.local/share/pi-extensions/rlm-kernel-v1`; preparation and isolated installation
verification are separate rollout steps, not accomplished by this documentation.
The extension is now named `rlm` at `packages/rlm`. The existing verified clone
keeps its historical directory name and commit until an intentional update;
this source rename does not migrate that clone or personal package settings.
See the [RLM installation guide](packages/rlm/README.md#installation)
for commit pinning and removal.

Once that clone is prepared:

```bash
pi install "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
pi list
# When you choose to uninstall:
pi remove "$HOME/.local/share/pi-extensions/rlm-kernel-v1"
```

Pi loads all four extensions declared by the root `package.json`. These commands
change personal package settings; they are instructions for an intentional install,
not an instruction to migrate an existing development installation. Removal does
not erase kernel state. This private package needs no npm publication, and direct
`git:file://...` installation was rejected by pi 1.1.0.

## Support

The v1 kernel support target is Linux with pi 1.1.0, using its `model-only`,
`ctx.tools`, `ctx.executeTool`, and session lifecycle APIs. The bounded suite was
run on Linux x86_64 with Node 26.11.1, CPython 3.13.16, uv 0.12.23 and the
Monty 1.1.0 dependency lock. The package declares Node >=22 and the sidecar declares
Python >=3.10; these minima are dependency constraints, not proof of every version
or platform. Earlier/later pi releases and other platforms are not claimed tested.
Host-provided pi packages, `pi-tui` and `typebox` are unbundled `"*"` peers per pi's package
contract; that peer range is not an API compatibility guarantee.

Read the [kernel operational contract](packages/rlm/README.md) before use:
bootstrap begins at session start, checkpoint files are private but unencrypted,
and reset/uninstall do not erase all retained sensitive files.

## Test

From the repository root, with `pi`, Node, Python and `uv` on `PATH`:

```bash
timeout 180s python3 packages/rlm/isolated-check.py --timeout 120 -- npm test
timeout 60s python3 packages/rlm/isolated-check.py --timeout 45 -- python3 -B packages/rlm/package-check.py
timeout 20s python3 -B packages/rlm/package_check_tests.py
```

The Linux isolation runner owns private agent configuration, working directory,
sessions, state, venvs, and bounded descendant cleanup; it does not load the
personal installation or credentials. It reuses the uv dependency cache, so a
cold cache can still require dependency downloads. No paid model test runs by
default. The structural check verifies tracked/packed runtime and lock files,
local documentation links, discovery metadata, host peers and file exclusions.
Registration accepts only the seven expected executable argv in order, joined by
`&&`, with spaces or tabs between literal words. Quotes, comments, conditionals,
redirection and other shell syntax fail closed; this is not a general shell parser.
Intentional test-chain changes must update the checker contract. The retained
registration regressions use private manifest copies and never execute mutated
script text; npm artifact checks use private config files and cache.
Structural checks do not prove narrative correctness or installed sidecar readiness.
Isolated installation/removal and opt-in paid lifecycle verification remain
separate gates.
