#!/usr/bin/env python3
"""Check package/docs structure, not narrative accuracy or installed readiness."""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KERNEL = "packages/rlm/"
REQUIRED = {
    "package.json", "README.md",
    *(KERNEL + path for path in (
        "README.md", "index.ts", "render.ts", "render-probe.ts", "render-check.py", "client.mjs", "bridge.mjs", "package-check.py",
        "package_check_tests.py", "installed-generation.mjs", "installed-generation-check.mjs",
        "python/sidecar.py", "python/pyproject.toml", "python/uv.lock",
        "python/check.py", "python/bootstrap_tests.py", "python/jupyter_sidecar_tests.py",
        "python/cancellation_tests.py", "python/operations_tests.py",
        "check.mjs", "bridge-check.mjs", "integration.py", "e2e.py",
        "lifecycle.py", "lifecycle_controls.py", "lifecycle-probe.ts", "lifecycle-replay-probe.ts",
        "permissions.py", "permissions_tests.py", "permissions_assembly_tests.py", "permissions-probe.mjs",
        "permissions_replay.py", "permissions_replay_tests.py", "permissions-replay-probe.ts",
        "legacy_state_sensor.py", "legacy_state_sensor_tests.py",
        "completion_budget.py", "completion_budget_tests.py",
        "install-check.py", "index-probe.ts", "installed-probe.ts",
        "isolated-check.py", "isolated_check_tests.py", "self_test_controls.py",
    )),
}
HOST_PACKAGES = {"@earendil-works/pi-ai", "@earendil-works/pi-coding-agent", "@earendil-works/pi-tui", "typebox"}
STATE_FILES = ("kernel.bin", "kernel.names.json", "kernel.names.tmp", "kernel.tmp", "kernel.corrupt")
# Only this literal executable chain is supported, not arbitrary shell syntax.
TEST_ARGV = tuple(tuple(program.split()) for program in (
    "node packages/bedrock-sso/inject.mjs",
    "node packages/cache-status/stats.mjs",
    "node packages/beads-reminder/detect.mjs",
    "uv run --project packages/rlm/python --locked python packages/rlm/python/check.py",
    "node packages/rlm/check.mjs",
    "node packages/rlm/bridge-check.mjs",
    "python3 packages/rlm/integration.py",
))


def command(*args):
    # npm's user/global config and cache stay private even outside the pi runner.
    with tempfile.TemporaryDirectory(prefix="pi-package-check-command-") as directory:
        env = dict(os.environ)
        if args[0] == "npm":
            env = {key: value for key, value in env.items() if not key.lower().startswith("npm_")}
            for name in ("userconfig", "globalconfig"):
                config = Path(directory) / name
                config.touch()
                env["npm_config_" + name] = str(config)
            env["npm_config_cache"] = directory
        return subprocess.run(args, cwd=ROOT, env=env, text=True, capture_output=True,
                              check=True, timeout=30).stdout


def test_registration(script):
    assert isinstance(script, str), "test registration: missing scripts.test command"
    # Splitting horizontal whitespace preserves every shell metacharacter,
    # quote and newline as a literal mismatch. Never evaluate the script text.
    argv = tuple(tuple(re.split(r"[ \t]+", part.strip(" \t"))) for part in script.split("&&"))
    assert argv == TEST_ARGV, "test registration: expected the seven executable argv in a simple && chain"


def metadata():
    package = json.loads((ROOT / "package.json").read_text())
    entries = {path.relative_to(ROOT).as_posix() for pattern in package["pi"]["extensions"]
               for path in ROOT.glob(pattern)}
    assert KERNEL + "index.ts" in entries, "kernel missing from root pi manifest"
    peers = package["peerDependencies"]
    assert all(peers.get(name) == "*" for name in HOST_PACKAGES), "host peers must use *"
    assert not HOST_PACKAGES.intersection(package.get("dependencies", {})), "host packages bundled"
    test_registration(package.get("scripts", {}).get("test"))
    return f"{len(entries)} extension entries; 4 host peers; 4 kernel test programs"


def documentation():
    required_links = {
        "README.md": {"packages/rlm/"},
        KERNEL + "README.md": {"../../README.md"},
    }
    count = 0
    for source in ("README.md", KERNEL + "README.md"):
        # These docs use ordinary inline links. Ignore web links and anchor-only links.
        links = set(re.findall(r"\[[^\]]*\]\(([^)\s]+)\)", (ROOT / source).read_text()))
        assert required_links.get(source, set()) <= links, f"missing documentation links: {source}"
        for link in links:
            if ":" in link or link.startswith("#"):
                continue
            path = (ROOT / source).parent / link.split("#", 1)[0]
            assert path.exists(), f"broken link: {source} -> {link}"
            count += 1
    return f"{count} local links resolve"


def source_paths(mode):
    if mode == "committed":
        return set(command("git", "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    paths = set(command("git", "ls-files").splitlines())
    paths.update(path.relative_to(ROOT).as_posix() for path in ROOT.rglob("*")
                 if path.is_file() and ".git" not in path.parts and ".venv" not in path.parts
                 and "__pycache__" not in path.parts)
    return paths


def tracked(mode):
    paths = source_paths(mode)
    missing = REQUIRED - paths
    assert not missing, f"missing required paths: {sorted(missing)}"
    if mode == "working-tree":
        for name in REQUIRED:
            path = ROOT / name
            assert path.is_file() and not path.is_symlink(), f"unsafe working-tree source: {name}"
    return f"{len(REQUIRED)} required {mode} paths"


def exclusions():
    paths = [KERNEL + "python/.venv/sentinel", KERNEL + "python/__pycache__/sentinel.pyc",
             *(KERNEL + name for name in STATE_FILES)]
    ignored = set(command("git", "check-ignore", "--no-index", *paths).splitlines())
    assert set(paths) <= ignored, f"not ignored: {sorted(set(paths) - ignored)}"
    return f"{len(paths)} venv/cache/state paths ignored"


def artifact():
    reports = json.loads(command("npm", "pack", "--dry-run", "--json", "--ignore-scripts"))
    report, = reports.values() if isinstance(reports, dict) else reports
    paths = {item["path"] for item in report["files"]}
    assert REQUIRED <= paths, f"missing artifact paths: {sorted(REQUIRED - paths)}"
    forbidden = {path for path in paths if set(Path(path).parts).intersection({".venv", "__pycache__"})
                 or Path(path).name in STATE_FILES or path.endswith(".pyc")}
    assert not forbidden, f"private/generated artifact paths: {sorted(forbidden)}"
    return f"{len(paths)} files; {report['size']} packed bytes; {report['unpackedSize']} unpacked bytes"


if __name__ == "__main__":
    mode = "committed"
    if sys.argv[1:] == ["--source", "working-tree"]:
        mode = "working-tree"
    elif sys.argv[1:] not in ([], ["--source", "committed"]):
        raise SystemExit("usage: package-check.py [--source committed|working-tree]")
    failures = 0
    checks = ((metadata.__name__, metadata), (documentation.__name__, documentation),
              ("tracked", lambda: tracked(mode)), (exclusions.__name__, exclusions), (artifact.__name__, artifact))
    for name, check in checks:
        try:
            print(f"PASS {name}: {check()}")
        except (AssertionError, subprocess.SubprocessError) as error:
            failures += 1
            print(f"FAIL {name}: {error}", file=sys.stderr)
    print(f"package boundary checks: {5 - failures}/5 passed")
    sys.exit(bool(failures))
