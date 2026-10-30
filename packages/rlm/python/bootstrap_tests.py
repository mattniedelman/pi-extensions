import importlib
import importlib.metadata
import importlib.util
import subprocess
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parent
PROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
SIDECAR = ROOT / "sidecar.py"

PRODUCTION_PINS = {
    "ipykernel": "6.30.1",
    "jupyter-client": "8.6.3",
    "pyzmq": "27.2.0",
}
PREPARED_PACKAGES = {
    "ipykernel": ("ipykernel", "6.30.1"),
    "jupyter-client": ("jupyter_client", "8.6.3"),
    "pyzmq": ("zmq", "27.2.0"),
    "numpy": ("numpy", "2.3.4"),
}
BOOTSTRAP_INVENTORY = {
    "test_exact_project_and_lock_roots",
    "test_prepared_imports_and_versions",
    "test_missing_jupyter_client_error_is_actionable",
    "test_kernel_start_error_is_actionable_and_keeps_cause",
    "test_kernel_readiness_error_is_actionable_and_cleans_up",
    "test_sidecar_startup_failure_exits_cleanly_with_open_stdin",
    "test_fixed_zero_skip_inventory",
}
JUPYTER_INVENTORY = {
    "test_01_provenance_numpy_and_retained_objects",
    "test_02_stdout_stderr_expression_and_display_data",
    "test_03_non_json_literals_fall_back_to_text_plain",
    "test_04_rich_mime_is_explicitly_unsupported",
    "test_05_input_fails_without_hanging",
    "test_06_fields_are_bounded_and_error_keeps_mutation",
    "test_unmapped_call_waits_for_single_mapping_then_dispatches",
    "test_wrong_identity_never_dispatches",
    "test_revoke_wakes_waiter_and_discards_late_result",
    "test_revoked_registry_entries_do_not_grow",
    "test_bridge_close_wakes_and_joins_accept_thread",
    "test_duplicate_unknown_malformed_oversize_and_flood_revoke",
    "test_serialized_frame_exact_boundaries_and_escaping",
    "test_generation_and_ids_do_not_reuse",
    "test_abort_revokes_waiting_tool_call_and_discards_late_result",
    "test_reset_revokes_waiting_tool_call_and_discards_late_result",
    "test_close_revokes_waiting_tool_call_and_discards_late_result",
    "test_actual_kernel_readiness_failure_cleans_up_sidecar",
    "test_partial_startup_failures_clean_up_started_resources",
    "test_cleanup_failures_are_notes_on_primary_startup_error",
    "test_reset_shuts_down_old_kernel_when_channel_cleanup_fails",
    "test_ignores_unrelated_messages_and_requires_matching_reply_and_idle",
    "test_matching_error_is_reported_after_both_completion_boundaries",
    "test_many_large_chunks_stay_bounded_while_draining_to_idle",
    "test_synchronous_execute_submission_failure_revokes_allocated_cell",
}
OPERATIONS_INVENTORY = {
    "test_variables_are_sorted_safe_and_do_not_pollute_or_read_values",
    "test_variables_use_one_correlated_silent_false_history_free_execution",
    "test_variables_truncate_300_ascii_names_at_256",
    "test_variables_compact_envelope_respects_exact_utf8_byte_boundary",
    "test_variables_reject_malformed_or_untrusted_result",
    "test_reset_rotates_generation_and_returns_exact_notice",
    "test_checkpoint_is_explicitly_unsupported",
    "test_sidecar_state_argument_is_optional_ignored_compatibility",
}
CANCELLATION_INVENTORY = {
    "test_matching_idle_after_interrupt_retains_generation_and_namespace",
    "test_no_idle_uses_one_public_bounded_shutdown_and_rotates_generation",
    "test_shutdown_cleanup_failure_is_terminal_and_disables_restart",
    "test_shutdown_deadline_is_terminal",
    "test_retirement_deadline_covers_shutdown_and_replacement_startup",
    "test_real_interrupt_retains_pid_generation_object_and_holds_next_cell",
    "test_sigint_ignore_forces_bounded_retirement_without_touching_sentinel",
    "test_missing_sigint_ignore_does_not_force_retirement",
    "test_interrupt_before_readiness_does_not_force_retirement",
}


def test_names(module):
    return {
        name
        for value in vars(module).values()
        if isinstance(value, type) and issubclass(value, unittest.TestCase)
        for name in unittest.defaultTestLoader.getTestCaseNames(value)
    }


class BootstrapTests(unittest.TestCase):
    def test_exact_project_and_lock_roots(self):
        project = tomllib.loads(PROJECT.read_text())["project"]
        self.assertEqual(project["requires-python"], ">=3.13,<3.14")
        self.assertEqual(
            project["dependencies"],
            [f"{name}=={version}" for name, version in PRODUCTION_PINS.items()],
        )

        lock = tomllib.loads(LOCK.read_text())
        self.assertEqual(lock["requires-python"], "==3.13.*")
        packages = {package["name"]: package for package in lock["package"]}
        root = packages["pi-rlm-sidecar"]
        self.assertEqual(
            {dependency["name"] for dependency in root["dependencies"]},
            set(PRODUCTION_PINS),
        )
        self.assertEqual(
            {item["name"]: item["specifier"] for item in root["metadata"]["requires-dist"]},
            {name: f"=={version}" for name, version in PRODUCTION_PINS.items()},
        )
        self.assertEqual(
            {name: packages[name]["version"] for name in PRODUCTION_PINS},
            PRODUCTION_PINS,
        )
        self.assertNotIn("numpy", {dependency["name"] for dependency in root["dependencies"]})
        self.assertNotIn("pydantic-monty", packages)

    def test_prepared_imports_and_versions(self):
        for distribution, (module, version) in PREPARED_PACKAGES.items():
            with self.subTest(distribution=distribution):
                self.assertIsNotNone(importlib.import_module(module))
                self.assertEqual(importlib.metadata.version(distribution), version)
        with self.assertRaises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("pydantic-monty")

    def test_missing_jupyter_client_error_is_actionable(self):
        real_import = __import__

        def missing(name, *args, **kwargs):
            if name == "jupyter_client":
                error = ModuleNotFoundError("No module named 'jupyter_client'")
                error.name = "jupyter_client"
                raise error
            return real_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("sidecar_missing_jupyter_client", SIDECAR)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", side_effect=missing), self.assertRaisesRegex(
            RuntimeError, r"jupyter_client.*locked.*dependencies"
        ):
            spec.loader.exec_module(module)

    def test_kernel_start_error_is_actionable_and_keeps_cause(self):
        import sidecar

        class Manager:
            def start_kernel(self):
                raise FileNotFoundError("No such kernel named python3")

            def shutdown_kernel(self, now=False):
                pass

        with self.assertRaisesRegex(
            RuntimeError, r"kernel startup.*ipykernel.*locked.*dependencies.*No such kernel named python3"
        ) as caught:
            sidecar.Kernel(Path("ignored"), manager_factory=Manager)
        self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)

    def test_kernel_readiness_error_is_actionable_and_cleans_up(self):
        import sidecar

        class Client:
            def __init__(self):
                self.stopped = False

            def start_channels(self):
                pass

            def stop_channels(self):
                self.stopped = True

            def wait_for_ready(self, timeout=None):
                raise TimeoutError("not ready")

        class Manager:
            def __init__(self):
                self.client = Client()
                self.shutdown = False

            def start_kernel(self):
                pass

            def blocking_client(self):
                return self.client

            def shutdown_kernel(self, now=False):
                self.shutdown = now

        manager = Manager()
        with self.assertRaisesRegex(RuntimeError, r"readiness.*ipykernel.*locked.*dependencies"):
            sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager)
        self.assertTrue(manager.client.stopped)
        self.assertTrue(manager.shutdown)

    def test_sidecar_startup_failure_exits_cleanly_with_open_stdin(self):
        source = "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(ROOT)!r})",
                "import sidecar",
                "class FailingKernel:",
                "    def __init__(self, *args, **kwargs):",
                "        raise RuntimeError('forced startup failure')",
                "sidecar.Kernel = FailingKernel",
                "sidecar.main()",
            ]
        )
        process = subprocess.Popen(
            [sys.executable, "-W", "error::ResourceWarning", "-c", source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        def cleanup_process():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()

        self.addCleanup(cleanup_process)
        self.assertEqual(process.wait(timeout=5), 1)
        assert process.stdout is not None
        assert process.stderr is not None
        self.assertEqual(process.stdout.read(), "")
        stderr = process.stderr.read()
        self.assertIn("RuntimeError: forced startup failure", stderr)
        self.assertNotIn("Fatal Python error", stderr)
        self.assertNotIn("ResourceWarning", stderr)

    def test_fixed_zero_skip_inventory(self):
        bootstrap = sys.modules[__name__]
        import cancellation_tests
        import jupyter_sidecar_tests
        import operations_tests

        self.assertEqual(test_names(bootstrap), BOOTSTRAP_INVENTORY)
        self.assertEqual(test_names(jupyter_sidecar_tests), JUPYTER_INVENTORY)
        self.assertEqual(test_names(cancellation_tests), CANCELLATION_INVENTORY)
        self.assertEqual(test_names(operations_tests), OPERATIONS_INVENTORY)
        combined = unittest.TestSuite(
            [
                unittest.defaultTestLoader.loadTestsFromModule(bootstrap),
                unittest.defaultTestLoader.loadTestsFromModule(jupyter_sidecar_tests),
                unittest.defaultTestLoader.loadTestsFromModule(cancellation_tests),
                unittest.defaultTestLoader.loadTestsFromModule(operations_tests),
            ]
        )
        self.assertEqual(combined.countTestCases(), 49)
        self.assertNotIn("skip", PROJECT.read_text().lower())


if __name__ == "__main__":
    unittest.main()
