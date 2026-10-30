import base64
import gc
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
import jupyter_sidecar_tests as support
import sidecar


class OperationTests(unittest.TestCase):
    kernel: sidecar.Kernel | None = None

    @property
    def active_kernel(self) -> sidecar.Kernel:
        assert self.kernel is not None
        return self.kernel

    def setUp(self):
        gc.collect()
        self.kernel = sidecar.Kernel(Path("ignored"))

    def tearDown(self):
        self.active_kernel.close()

    def execute(self, code):
        return self.active_kernel.execute("execute", code, [])

    def test_variables_are_sorted_safe_and_do_not_pollute_or_read_values(self):
        self.execute(
            "class Hostile:\n"
            "    def __repr__(self): raise RuntimeError('repr called')\n"
            "class HostileKey(str): pass\n"
            "zeta = Hostile()\nalpha = 1\n_hidden = 2\nglobals()[HostileKey('evil')] = 3"
        )
        before = self.execute("sorted(k for k in globals() if type(k) is str and not k.startswith('_') and k != 'pi')")["result"]
        self.execute(
            "globals = lambda: {'forged': 1}\n"
            "type = lambda value: str\n"
            "sorted = lambda value: ['forged']\n"
            "len = lambda value: 0\n"
            "get_ipython().display_formatter.formatters['text/plain'].for_type(str, lambda value, printer, cycle: printer.text('forged'))"
        )
        result = self.active_kernel.variables("variables")
        self.execute("get_ipython().display_formatter.formatters['text/plain'].pop(str)")
        after = self.execute("__import__('builtins').sorted(k for k in get_ipython().user_ns if __import__('builtins').type(k) is str and not k.startswith('_') and k != 'pi')")["result"]
        expected = sorted([*before, "globals", "len", "sorted", "type"])
        self.assertEqual(result, {"names": expected, "truncated": False})
        self.assertEqual(after, expected)
        self.assertNotIn("evil", result["names"])

    def test_variables_use_one_correlated_silent_false_history_free_execution(self):
        message = support.ProtocolCorrelationTests.message
        payload = base64.b64encode(b'{"names":["a"],"truncated":false}').decode()
        manager = support.FakeManager(
            [message("execute_reply", "wanted", status="ok")],
            [
                message("stream", "other", name="stdout", text="wrong"),
                message("display_data", "wanted", data={"application/vnd.pi.variables+json": "**PI_VARIABLES**" + payload}),
                message("status", "wanted", execution_state="idle"),
            ],
        )
        kernel = sidecar.Kernel(Path("ignored"), manager_factory=lambda: manager)
        try:
            self.assertEqual(kernel.variables("request"), {"names": ["a"], "truncated": False})
            self.assertEqual(manager.client_instance.execute_kwargs, {
                "allow_stdin": False, "store_history": False, "stop_on_error": True, "silent": False,
            })
            self.assertEqual(manager.client_instance.shell_channel.reads, 1)
            self.assertEqual(manager.client_instance.iopub_channel.reads, 3)
        finally:
            kernel.close()

    def test_variables_truncate_300_ascii_names_at_256(self):
        self.execute("globals().update({f'name_{i:03d}': i for i in range(300)})")
        result = self.active_kernel.variables("variables")
        self.assertEqual(len(result["names"]), 256)
        self.assertEqual(result["names"], sorted(result["names"]))
        visible = [name for name in result["names"] if name.startswith("name_")]
        self.assertEqual(visible, [f"name_{i:03d}" for i in range(len(visible))])
        self.assertTrue(result["truncated"])

    def test_variables_compact_envelope_respects_exact_utf8_byte_boundary(self):
        self.execute("globals().update({('n' + '\N{SNOWMAN}' * 100 + f'{i:03d}'): i for i in range(300)})")
        result = self.active_kernel.variables("variables")
        envelope = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(envelope), 16384)
        self.assertTrue(result["truncated"])
        next_name = "n" + "\N{SNOWMAN}" * 100 + f"{len(result['names']):03d}"
        too_large = json.dumps(
            {"names": [*result["names"], next_name], "truncated": True},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertGreater(len(too_large), 16384)

    def test_variables_reject_malformed_or_untrusted_result(self):
        cases = [
            {"text/plain": "**PI_VARIABLES**wrong-mime"},
            {"application/vnd.pi.variables+json": "wrong"},
            {"application/vnd.pi.variables+json": "**PI_VARIABLES**not-base64"},
            {"application/vnd.pi.variables+json": "**PI_VARIABLES**" + base64.b64encode(b"[]").decode()},
            {"application/vnd.pi.variables+json": "**PI_VARIABLES**" + base64.b64encode(b'{"names":["a","a"],"truncated":false}').decode()},
        ]
        for data in cases:
            with self.subTest(data=data):
                manager = support.FakeManager(
                    [support.ProtocolCorrelationTests.message("execute_reply", "wanted", status="ok")],
                    [
                        support.ProtocolCorrelationTests.message("display_data", "wanted", data=data),
                        support.ProtocolCorrelationTests.message("status", "wanted", execution_state="idle"),
                    ],
                )
                kernel = sidecar.Kernel(Path("ignored"), manager_factory=lambda manager=manager: manager)
                try:
                    with self.assertRaisesRegex(RuntimeError, "variables protocol"):
                        kernel.variables("request")
                finally:
                    kernel.close()

    def test_reset_rotates_generation_and_returns_exact_notice(self):
        old_generation = self.active_kernel.generation
        result = self.active_kernel.reset()
        self.assertEqual(result, {
            "reset": True,
            "state_lost": True,
            "notice": "Python workspace reset; previous live state was discarded",
        })
        self.assertNotEqual(self.active_kernel.generation, old_generation)

    def test_checkpoint_is_explicitly_unsupported(self):
        with self.assertRaisesRegex(
            ValueError,
            r"^Python checkpoints are unsupported; state lasts only for the live pi session\.$",
        ):
            sidecar.dispatch_operation(self.active_kernel, "request", "checkpoint", "", [])

    def test_sidecar_state_argument_is_optional_ignored_compatibility(self):
        parser = sidecar.argument_parser()
        self.assertIsNone(parser.parse_args([]).state)
        self.assertEqual(parser.parse_args(["--state", "legacy.bin"]).state, Path("legacy.bin"))


if __name__ == "__main__":
    unittest.main()
