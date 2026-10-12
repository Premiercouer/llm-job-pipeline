import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import EXP, REPO, needs_numpy


class HistoricalAnalysisTest(unittest.TestCase):
    """The published numbers must follow from the published data (standard library only)."""

    def test_rag_dev_analysis_reproduces_headline_numbers(self):
        sys.path.insert(0, str(EXP / "scripts"))
        import analyze_dev_results as a
        A = a.analyze(*a.load())
        self.assertEqual(A["n_jobs"], 6)
        self.assertAlmostEqual(A["context_reduction_vs_A"]["B"], 0.68, places=2)
        self.assertEqual(A["sequential_batch_model_EST"]["smallest_N_where_A_is_cheaper"]["B"], 5)
        self.assertEqual(A["retrieval_proxy"]["B_v1"]["L_or_A_project_in_context"], [6, 12])
        self.assertEqual(A["retrieval_proxy"]["B_prime_v2"]["L_or_A_project_in_context"], [9, 12])
        self.assertEqual(A["paired_vs_A"]["B"]["jobs_faster"], 5)

    def test_gate_analysis_reproduces_headline_numbers(self):
        p = subprocess.run([sys.executable, str(REPO / "experiments/two_stage_gate/scripts/analyze_gate.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_published_data_has_no_text_columns(self):
        cols = (EXP / "results/historical_dev/generations.csv").read_text().splitlines()[0].split(",")
        for banned in ("position", "company", "jd", "resume", "path", "url", "title", "anchor", "reason"):
            self.assertFalse([c for c in cols if banned in c.lower()], banned)


@needs_numpy
class DemoTest(unittest.TestCase):
    def test_offline_demo_runs_and_flags_every_planted_defect(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = subprocess.run([sys.executable, str(EXP / "scripts" / "demo.py"), "--out", tmp, "--embedding", "hashing"],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("demo OK", p.stdout)
            planted = json.loads((Path(tmp) / "rag_planted_defects.json").read_text())
            self.assertEqual(len(planted), 5)

    def test_generate_without_execute_makes_no_call(self):
        p = subprocess.run([sys.executable, str(EXP / "scripts" / "generate.py"), "--job",
                            str(EXP / "fixtures" / "illustrative_job.json"), "--mode", "rag"],
                           capture_output=True, text=True, env={"PATH": "/nonexistent", "CLAUDE_BIN": "/nonexistent"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("dry run: no model call made", p.stdout)


if __name__ == "__main__":
    unittest.main()
