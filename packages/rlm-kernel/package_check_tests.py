#!/usr/bin/env python3
"""Metadata-only registration regressions; never execute manifest script text."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "packages/rlm-kernel/package-check.py"
PACKAGE = json.loads((ROOT / "package.json").read_text())
SCRIPT = PACKAGE["scripts"]["test"]
KERNEL_COMMANDS = (
    "uv run --project packages/rlm-kernel/python --locked python packages/rlm-kernel/python/check.py",
    "node packages/rlm-kernel/check.mjs",
    "node packages/rlm-kernel/bridge-check.mjs",
    "python3 packages/rlm-kernel/integration.py",
)


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
        spec = importlib.util.spec_from_file_location("package_registration_fixture", checker)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
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
        spec = importlib.util.spec_from_file_location("package_artifact_contract", CHECKER)
        checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(checker)
        paths = {"packages/rlm-kernel/" + name for name in
                 ("permissions.py", "permissions_tests.py", "permissions_assembly_tests.py", "permissions-probe.mjs",
                  "permissions_replay.py", "permissions-replay-probe.ts")}
        self.assertTrue(paths <= checker.REQUIRED, "permission oracle missing from required artifact contract")

    def test_actual_seven_program_manifest(self):
        self.assertEqual(len(SCRIPT.split(" && ")), 7)
        self.assertEqual(metadata_for(SCRIPT), "4 extension entries; 3 host peers; 4 kernel test programs")

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
