#!/usr/bin/env python3
"""Metadata-only registration regressions; never execute manifest script text."""
import importlib.util
import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "packages/rlm/package-check.py"
PACKAGE = json.loads((ROOT / "package.json").read_text())
SCRIPT = PACKAGE["scripts"]["test"]
KERNEL_COMMANDS = (
    "uv run --project packages/rlm/python --locked python packages/rlm/python/check.py",
    "node packages/rlm/check.mjs",
    "node packages/rlm/bridge-check.mjs",
    "python3 packages/rlm/integration.py",
)


def load_checker(name, path=CHECKER):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load checker: {path}")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def metadata_for(script):
    """Load the real checker against a tiny private manifest/discovery copy."""
    directory = tempfile.TemporaryDirectory(prefix="pi-package-registration-")
    root = Path(directory.name)
    try:
        checker = root / CHECKER.relative_to(ROOT)
        checker.parent.mkdir(parents=True)
        shutil.copyfile(CHECKER, checker)
        package = json.loads(json.dumps(PACKAGE))
        package["scripts"]["test"] = script
        (root / "package.json").write_text(json.dumps(package))
        for pattern in PACKAGE["pi"]["extensions"]:
            for source in ROOT.glob(pattern):
                path = root / source.relative_to(ROOT)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
        module = load_checker("package_registration_fixture", checker)
        # Only metadata(), never npm/shell or artifact checks, consumes the mutation.
        return module.metadata()
    finally:
        directory.cleanup()
        assert not root.exists(), "private registration fixture survived cleanup"


class RegistrationTests(unittest.TestCase):
    def reject(self, script):
        with self.assertRaisesRegex(AssertionError, "test registration:"):
            metadata_for(script)

    def test_permission_oracle_is_required_in_artifact(self):
        checker = load_checker("permission_artifact_contract")
        paths = {"packages/rlm/" + name for name in
                 ("permissions.py", "permissions_tests.py", "permissions_assembly_tests.py", "permissions-probe.mjs",
                  "permissions_replay.py", "permissions_replay_tests.py", "permissions-replay-probe.ts")}
        self.assertTrue(paths <= checker.REQUIRED, "permission oracle missing from required artifact contract")

    def test_legacy_state_sensor_is_required_in_artifact(self):
        checker = load_checker("legacy_sensor_artifact_contract")
        paths = {"packages/rlm/legacy_state_sensor.py", "packages/rlm/legacy_state_sensor_tests.py"}
        self.assertTrue(paths <= checker.REQUIRED, "legacy-state sensor missing from artifact contract")

    def test_lifecycle_adapter_is_required_in_artifact(self):
        checker = load_checker("lifecycle_artifact_contract")
        paths = {"packages/rlm/" + name for name in
                 ("lifecycle.py", "lifecycle_controls.py", "lifecycle-probe.ts", "lifecycle-replay-probe.ts")}
        self.assertTrue(paths <= checker.REQUIRED, "lifecycle adapter missing from artifact contract")

    def test_renderer_is_required_in_artifact(self):
        checker = load_checker("renderer_artifact_contract")
        self.assertTrue({"packages/rlm/render.ts", "packages/rlm/render-probe.ts", "packages/rlm/render-check.py"} <= checker.REQUIRED)

    def test_self_test_controls_are_required_in_artifact(self):
        checker = load_checker("self_test_artifact_contract")
        self.assertIn("packages/rlm/self_test_controls.py", checker.REQUIRED)

    def test_rlm_package_name_and_sidecar_lock(self):
        self.assertTrue((ROOT / "packages/rlm/index.ts").is_file())
        self.assertFalse((ROOT / "packages/rlm-kernel/index.ts").exists())
        self.assertNotIn("packages/rlm-kernel/", SCRIPT)
        project_text = (ROOT / "packages/rlm/python/pyproject.toml").read_text()
        lock_text = (ROOT / "packages/rlm/python/uv.lock").read_text()
        project = tomllib.loads(project_text)["project"]
        lock = tomllib.loads(lock_text)
        self.assertEqual(project["name"], "pi-rlm-sidecar")
        self.assertEqual(lock["requires-python"], "==3.13.*")
        self.assertNotIn('name = "pi-rlm-kernel-sidecar"', project_text + lock_text)
        self.assertEqual(project["requires-python"], ">=3.13,<3.14")
        self.assertEqual(
            project["dependencies"],
            ["ipykernel==6.30.1", "jupyter-client==8.6.3", "pyzmq==27.2.0"],
        )
        packages = {package["name"]: package for package in lock["package"]}
        root = packages["pi-rlm-sidecar"]
        pins = {"ipykernel": "6.30.1", "jupyter-client": "8.6.3", "pyzmq": "27.2.0"}
        self.assertEqual(
            {item["name"]: item["specifier"] for item in root["metadata"]["requires-dist"]},
            {name: f"=={version}" for name, version in pins.items()},
        )
        self.assertEqual({name: packages[name]["version"] for name in pins}, pins)
        self.assertNotIn("pydantic-monty", project_text + lock_text)
        self.assertEqual(metadata_for(SCRIPT), "4 extension entries; 4 host peers; 4 kernel test programs")

    def test_actual_seven_program_manifest(self):
        self.assertEqual(len(SCRIPT.split(" && ")), 7)
        self.assertEqual(metadata_for(SCRIPT), "4 extension entries; 4 host peers; 4 kernel test programs")

    def test_direct_jupyter_sources_replace_monty_recovery_sources(self):
        checker = load_checker("direct_jupyter_artifact_contract")
        direct = {
            "packages/rlm/python/bootstrap_tests.py",
            "packages/rlm/python/jupyter_sidecar_tests.py",
            "packages/rlm/python/cancellation_tests.py",
            "packages/rlm/python/operations_tests.py",
        }
        obsolete = {
            "packages/rlm/stopped-worker-check.py",
            "packages/rlm/stopped-worker-probe.mjs",
            "packages/rlm/python/recovery_check.py",
            "packages/rlm/python/recovery_timeout_check.py",
            "packages/rlm/python/recovery_budget_tests.py",
        }
        self.assertTrue(direct <= checker.REQUIRED)
        self.assertTrue(all((ROOT / path).is_file() for path in direct))
        self.assertTrue(obsolete.isdisjoint(checker.REQUIRED))
        self.assertTrue(all(not (ROOT / path).exists() for path in obsolete))
        self.assertNotIn("stopped-worker", (ROOT / "packages/rlm/check.mjs").read_text())

    def test_working_tree_source_accepts_declared_new_files(self):
        checker = load_checker("working_tree_source_contract")
        self.assertIn("required working-tree paths", checker.tracked("working-tree"))
        with self.assertRaisesRegex(AssertionError, "missing required paths"):
            checker.tracked("committed")

    def test_working_tree_source_rejects_symlink(self):
        checker = load_checker("working_tree_symlink_contract")
        path = ROOT / "packages/rlm/python/operations_tests.py"
        with tempfile.TemporaryDirectory() as directory:
            original = path.read_bytes()
            path.unlink()
            path.symlink_to(Path(directory) / "missing")
            try:
                with self.assertRaisesRegex(AssertionError, "missing required paths|unsafe working-tree source"):
                    checker.tracked("working-tree")
            finally:
                path.unlink()
                path.write_bytes(original)

    def test_horizontal_whitespace_is_supported(self):
        self.assertEqual(metadata_for("\t" + SCRIPT.replace(" ", "\t") + "  "),
                         metadata_for(SCRIPT))

    def test_echo_of_all_required_paths(self):
        self.reject("echo " + " ".join(command.split()[-1] for command in KERNEL_COMMANDS))

    def test_missing_invocation(self):
        for command in KERNEL_COMMANDS:
            with self.subTest(command=command):
                self.reject(" && ".join(part for part in SCRIPT.split(" && ") if part != command))

    def test_comment_only_invocations(self):
        self.reject("# " + SCRIPT)

    def test_one_commented_invocation(self):
        self.reject(SCRIPT.replace(KERNEL_COMMANDS[-1], "# " + KERNEL_COMMANDS[-1]))

    def test_quoted_full_command_text(self):
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                self.reject("echo " + quote + SCRIPT + quote)

    def test_conditional_bypass(self):
        self.reject("true || " + SCRIPT)

    def test_node_syntax_check_is_not_execution(self):
        for command in KERNEL_COMMANDS[1:3]:
            with self.subTest(command=command):
                self.reject(SCRIPT.replace(command, command.replace("node ", "node --check ")))

    def test_wrong_python_or_uv_argv(self):
        for old, new in (("--locked ", ""), ("python3 packages/", "python3 -m packages/")):
            with self.subTest(argv=new):
                self.reject(SCRIPT.replace(old, new))

    def test_unsupported_shell_syntax(self):
        for script in (SCRIPT.replace(" && ", " ; "), SCRIPT.replace(" && ", " | "),
                       SCRIPT.replace(" && ", " & "), "(" + SCRIPT + ")",
                       "if false; then " + SCRIPT + "; fi", SCRIPT + " > result",
                       SCRIPT + "\n", SCRIPT + " # comment", "exit 0 && " + SCRIPT,
                       "$(echo ignored) && " + SCRIPT, "`echo ignored` && " + SCRIPT):
            with self.subTest(script=script):
                self.reject(script)

    def test_expected_path_in_extra_argument_is_not_execution(self):
        self.reject(SCRIPT.replace(KERNEL_COMMANDS[1],
                                  "node --eval '' " + KERNEL_COMMANDS[1].split()[-1]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
