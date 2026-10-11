"""The screening worker on the API backend (common.llm.AnthropicApiBackend against a fake SDK):
same validation, retry policy, guardrails and usage accounting as the CLI path.

Run: python3 -m unittest discover -s screening/tests -t .      (from the repository root)
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
os.environ["LJP_WORKSPACE"] = tempfile.mkdtemp(prefix="ljp_test_ws_")
os.environ["LJP_CANDIDATE_DIR"] = str(REPO / "examples" / "candidate")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "screening" / "scripts"))
import screening_core as core  # noqa: E402
from common import llm  # noqa: E402
from common.tests.test_llm import FakeClient, fake_sdk, message  # noqa: E402

JD = ("We are hiring a Data Analyst. You will build ETL pipelines in Python and SQL, maintain "
      "Power BI dashboards for operations, and clean messy multi-source data. French is an asset.")
JOB = {"source_key": "https://x.test/1", "position": "Data Analyst", "jd": JD, "platform": "greenhouse"}
REJECT = {"source_key": JOB["source_key"],
          "hard_reject": {"hit": True, "type": "LANGUAGE", "jd_anchor": "French is an asset"},
          "core_responsibilities": [], "anchor_direct_match": None,
          "main_gap": {"type": "STRUCTURAL", "description": "French"},
          "decision": "REJECT", "reason": "French is mentioned."}


class Replies(FakeClient):
    """Returns the queued replies in order (messages or exceptions)."""
    def __init__(self, replies):
        super().__init__(None)
        self.queue = list(replies)

    def _create(self, **params):
        self.requests.append(params)
        r = self.queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ApiWorkerTests(unittest.TestCase):
    def worker(self, replies):
        self.sdk, self.client = fake_sdk(), Replies(replies)
        backend = llm.AnthropicApiBackend(sdk=self.sdk, client=self.client)
        with mock.patch.object(llm, "make_backend", return_value=backend):
            w = core.Worker(core.WORKER_SINGLE_TURN, None, backend="api")
        self.sys_tokens = int(0.47 * len(w.system_prompt))
        core.stop_event.clear()
        return w

    def cold(self, text):  # first request writes the system prompt + job to the cache
        return message(text=text, cache_creation_input_tokens=self.sys_tokens + 600, cache_read_input_tokens=0)

    def test_valid_result_and_manifest(self):
        w = self.worker([])
        self.client.queue = [self.cold(json.dumps(REJECT))]
        before = core.metrics["invocations"]
        res = core.screen_with_retry(JOB, w, timeout=30)
        self.assertEqual(res["decision"], "REJECT")
        self.assertEqual(core.metrics["invocations"] - before, 1)
        m = w.manifest()
        self.assertEqual((m["backend"], m["effective_model"]), ("anthropic-api", "claude-opus-5-5"))
        req = self.client.requests[0]
        self.assertEqual(req["system"][0]["text"], w.system_prompt)  # the hashed, benchmarked prompt
        self.assertTrue(req["messages"][0]["content"].startswith(core.SINGLE_TURN_INSTRUCTION))

    def test_transient_error_is_retried_once(self):
        w = self.worker([])
        self.client.queue = [self.sdk.RateLimitError("slow down", 429), self.cold(json.dumps(REJECT))]
        self.assertEqual(core.screen_with_retry(JOB, w, timeout=30)["decision"], "REJECT")
        self.assertEqual(len(self.client.requests), 2)

    def test_invalid_output_is_not_retried(self):
        w = self.worker([])
        bad = dict(REJECT, hard_reject={"hit": True, "type": "LANGUAGE", "jd_anchor": "not in the JD"})
        self.client.queue = [self.cold(json.dumps(bad))]
        with self.assertRaises(core.NonRetryableJobError):
            core.screen_with_retry(JOB, w, timeout=30)
        self.assertEqual(len(self.client.requests), 1)

    def test_refusal_fails_only_this_job(self):
        w = self.worker([])
        self.client.queue = [message(stop="refusal")]
        with self.assertRaises(core.JobFatalError):
            core.screen_with_retry(JOB, w, timeout=30)
        self.assertFalse(core.stop_event.is_set())

    def test_auth_failure_stops_the_run(self):
        w = self.worker([])
        self.client.queue = [self.sdk.AuthenticationError("bad key", 401)]
        with self.assertRaises(core.GlobalFatalError):
            core.screen_with_retry(JOB, w, timeout=30)
        self.assertTrue(core.stop_event.is_set())
        core.stop_event.clear()

    def test_model_change_mid_run_trips_the_guardrail(self):
        w = self.worker([])
        warm = message(text=json.dumps(REJECT), model="claude-sonnet-5-5",
                       cache_creation_input_tokens=200, cache_read_input_tokens=self.sys_tokens)
        self.client.queue = [self.cold(json.dumps(REJECT)), warm]
        core.screen_with_retry(JOB, w, timeout=30)
        with self.assertRaises(core.GlobalFatalError):
            core.screen_with_retry(JOB, w, timeout=30)
        core.stop_event.clear()

    def test_hidden_context_trips_the_guardrail(self):
        w = self.worker([])
        self.client.queue = [message(text=json.dumps(REJECT), cache_creation_input_tokens=10 * self.sys_tokens)]
        with self.assertRaises(core.GlobalFatalError):
            core.screen_with_retry(JOB, w, timeout=30)
        core.stop_event.clear()

    def test_agent_mode_is_rejected(self):
        with self.assertRaises(core.PipelineError):
            core.Worker(core.WORKER_AGENT, None, backend="api")


if __name__ == "__main__":
    unittest.main()
