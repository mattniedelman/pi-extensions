import importlib.util
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("completion_budget.py")
SPEC = importlib.util.spec_from_file_location("completion_budget", MODULE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("failed to load completion_budget.py")
completion_budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(completion_budget)
validate = completion_budget.validate


class CompletionBudgetTests(unittest.TestCase):
    def test_boundaries(self):
        for value in (1048575, 1048576):
            self.assertEqual(validate(f"JUPYTER_SUITE elapsed=1.25 max_rss_kb={value} exit=0\n", 1048576)["max_rss_kb"], value)
        with self.assertRaisesRegex(ValueError, "exceeds"):
            validate("JUPYTER_SUITE elapsed=1.25 max_rss_kb=1048577 exit=0\n", 1048576)

    def test_rejects_bad_evidence(self):
        cases = (
            "",
            "JUPYTER_SUITE elapsed=nan max_rss_kb=1 exit=0\n",
            "JUPYTER_SUITE elapsed=1 max_rss_kb=1 exit=7\n",
            "JUPYTER_SUITE elapsed=1 max_rss_kb=x exit=0\n",
            "JUPYTER_SUITE elapsed=1 max_rss_kb=1 exit=0\nJUPYTER_SUITE elapsed=2 max_rss_kb=2 exit=0\n",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(ValueError):
                validate(text, 1048576)

    def test_file_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metrics"
            path.write_text("JUPYTER_SUITE elapsed=0.01 max_rss_kb=10 exit=0\n")
            self.assertEqual(validate(path.read_text(), 1048576)["exit"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
