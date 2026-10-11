import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "02_scan_greenhouse_source_b.py"
SPEC = importlib.util.spec_from_file_location("source_b_scan", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class SourceBTests(unittest.TestCase):
    def test_token_normalization_is_lightweight(self):
        self.assertEqual(MODULE.normalize_token(" /Acme-Board/ "), "acme-board")
        self.assertEqual(MODULE.normalize_token("a_b.c"), "a_b.c")

    def test_set_comparison(self):
        result = MODULE.source_sets({"a", "b"}, {"b", "c"})
        self.assertEqual(result["overlap"], {"b"})
        self.assertEqual(result["only_a"], {"a"})
        self.assertEqual(result["only_b"], {"c"})
        self.assertEqual(result["union"], {"a", "b", "c"})


if __name__ == "__main__":
    unittest.main()
