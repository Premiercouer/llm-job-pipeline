"""Offline tests for the single-turn context guardrail (screening_core.Worker._context_problems).

Run: python3 -m unittest discover -s screening/tests -t .      (from the repository root)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ["LJP_WORKSPACE"] = tempfile.mkdtemp(prefix="ljp_test_ws_")
os.environ["LJP_CANDIDATE_DIR"] = str(REPO / "examples" / "candidate")
sys.path.insert(0, str(REPO / "screening" / "scripts"))
import screening_core as core  # noqa: E402


def usage(inp=2, cw=0, cr=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}


class TestContextGuardrail(unittest.TestCase):
    def setUp(self):
        self.w = core.Worker(core.WORKER_SINGLE_TURN, "/usr/bin/true")
        self.sys_tokens = int(0.47 * len(self.w.system_prompt))  # benchmark ratio
        self.prompt = "x" * 2600

    def tearDown(self):
        self.w.close()

    def check(self, **u):
        return self.w._context_problems(usage(**u), self.prompt)

    def test_cold_then_warm_requests_pass(self):
        self.assertEqual(self.check(cw=self.sys_tokens + 1200), [])
        self.assertEqual(self.check(cw=600, cr=self.sys_tokens), [])
        self.assertEqual(self.check(cw=580, cr=self.sys_tokens), [])

    def test_repeated_request_reads_its_job_from_cache(self):
        """A retry of the same job within the cache TTL also reads part of the job prompt."""
        self.assertEqual(self.check(cw=600, cr=self.sys_tokens + 1200), [])   # repeat seen first
        self.assertEqual(self.check(cw=600, cr=self.sys_tokens), [])          # then a new job
        self.assertEqual(self.check(cw=10, cr=self.sys_tokens + 1200), [])    # then a retry

    def test_hidden_context_is_still_caught(self):
        """~7k tokens of injected context (e.g. a CLAUDE.md) exceed one job's prompt."""
        self.assertEqual(self.check(cw=600, cr=self.sys_tokens), [])
        self.assertTrue(self.check(cw=600, cr=self.sys_tokens + 7000))
        self.assertTrue(self.check(cw=7600, cr=self.sys_tokens))            # uncached part too big

    def test_cached_prefix_larger_than_any_explanation(self):
        self.assertTrue(self.check(cw=600, cr=3 * self.sys_tokens))


class TestPromptNotOnCommandLine(unittest.TestCase):
    def test_system_prompt_is_a_private_file(self):
        w = core.Worker(core.WORKER_SINGLE_TURN, "/usr/bin/true")
        try:
            self.assertIn("--system-prompt-file", w.cmd)
            self.assertNotIn("--system-prompt", w.cmd)
            self.assertFalse(any("Eligibility Facts" in a for a in w.cmd))
            self.assertEqual(os.stat(w.system_prompt_file).st_mode & 0o077, 0)
            self.assertEqual(os.listdir(w.run_cwd()), [])
        finally:
            w.close()
        self.assertFalse(Path(w.system_prompt_file).exists())


if __name__ == "__main__":
    unittest.main()
