"""Offline tests for the screening layer: ScreeningResult validation and JD grounding, blank-JD
short-circuit, CLI failure classification and cross-source duplicate detection.

Run: python3 -m unittest discover -s screening/tests -t .      (from the repository root)
"""
import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
os.environ["LJP_WORKSPACE"] = tempfile.mkdtemp(prefix="ljp_test_ws_")
os.environ["LJP_CANDIDATE_DIR"] = str(REPO / "examples" / "candidate")
SCRIPTS = REPO / "screening" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import screening_core as core  # noqa: E402

_spec = importlib.util.spec_from_file_location("merge_results", SCRIPTS / "03_merge_results.py")
merge = importlib.util.module_from_spec(_spec)
sys.modules["merge_results"] = merge
_spec.loader.exec_module(merge)

JD = ("We are hiring a Data Analyst. You will build ETL pipelines in Python and SQL, maintain "
      "Power BI dashboards for operations, and clean messy multi-source data. Requires 1+ years "
      "of experience. French is an asset.")
JOB = {"source_key": "https://x.test/1", "position": "Data Analyst", "jd": JD}


def result(**over):
    r = {"source_key": JOB["source_key"],
         "hard_reject": {"hit": False, "type": None, "jd_anchor": None},
         "core_responsibilities": [
             {"responsibility": "Build ETL", "jd_anchor": "build ETL pipelines in Python and SQL", "match": "DIRECT"},
             {"responsibility": "Dashboards", "jd_anchor": "maintain Power BI dashboards", "match": "DIRECT"},
             {"responsibility": "Data cleaning", "jd_anchor": "clean messy multi-source data", "match": "ADJACENT"}],
         "anchor_direct_match": "ETL pipelines in Python / SQL",
         "main_gap": {"type": "LEARNABLE", "description": "operations domain"},
         "decision": "ACCEPT", "reason": "Direct ETL and BI match."}
    r.update(over)
    return r


class TestValidateResult(unittest.TestCase):
    def test_valid_result(self):
        self.assertIsNone(core.validate_result(result(), JOB))

    def test_anchor_must_be_verbatim_jd_text(self):
        r = result()
        r["core_responsibilities"][0]["jd_anchor"] = "builds data pipelines with Python"  # paraphrase
        self.assertIn("not found in JD", core.validate_result(r, JOB))

    def test_anchor_tolerates_typography_and_whitespace(self):
        r = result()
        r["core_responsibilities"][0]["jd_anchor"] = "“build  ETL pipelines in Python and SQL”"
        self.assertIsNone(core.validate_result(r, JOB))

    def test_anchor_may_not_come_from_the_title(self):
        r = result()
        r["core_responsibilities"][0]["jd_anchor"] = "Data Analyst position"
        self.assertIn("not found in JD", core.validate_result(r, JOB))

    def test_hard_reject_requires_reject_and_grounding(self):
        hr = {"hit": True, "type": "LANGUAGE", "jd_anchor": "French is an asset"}
        self.assertIsNone(core.validate_result(result(hard_reject=hr, decision="REJECT",
                                                      core_responsibilities=[], anchor_direct_match=None,
                                                      main_gap={"type": "STRUCTURAL", "description": "French"}), JOB))
        self.assertIn("decision is not REJECT", core.validate_result(result(hard_reject=hr), JOB))
        bad = dict(hr, jd_anchor="Bilingual French required")
        self.assertIn("not found in JD", core.validate_result(result(hard_reject=bad, decision="REJECT"), JOB))

    def test_french_title_is_the_only_title_anchor(self):
        job = {**JOB, "position": "Analyste de données", "jd": "Rôle en analyse."}
        hr = {"hit": True, "type": "LANGUAGE", "jd_anchor": "Analyste de données"}
        r = result(hard_reject=hr, decision="REJECT", core_responsibilities=[], anchor_direct_match=None,
                   main_gap={"type": "STRUCTURAL", "description": "French"})
        self.assertIsNone(core.validate_result(r, job))
        r["hard_reject"] = {**hr, "type": "SENIORITY"}
        self.assertIn("not found in JD", core.validate_result(r, job))

    def test_direct_and_anchor_direct_match_are_consistent(self):
        self.assertIn("anchor_direct_match is null", core.validate_result(result(anchor_direct_match=None), JOB))
        r = result()
        for cr in r["core_responsibilities"]:
            cr["match"] = "ADJACENT"
        self.assertIn("no core responsibility is DIRECT", core.validate_result(r, JOB))

    def test_schema_and_enum_errors(self):
        self.assertIn("bad keys", core.validate_result({**result(), "extra": 1}, JOB))
        self.assertIn("source_key mismatch", core.validate_result(result(source_key="other"), JOB))
        self.assertIn("invalid decision", core.validate_result(result(decision="MAYBE"), JOB))
        r = result()
        r["core_responsibilities"] = r["core_responsibilities"][:2]
        self.assertIn("needs 3-5", core.validate_result(r, JOB))


class TestBlankJD(unittest.TestCase):
    def test_blank_jd_resolves_locally_without_claude(self):
        job = {**JOB, "jd": "   "}
        r = core.local_result(job)
        self.assertEqual(r["decision"], "UNABLE TO ASSESS")
        self.assertIsNone(core.validate_result(r, job))

    def test_blank_jd_with_french_title_goes_to_the_worker(self):
        self.assertIsNone(core.local_result({**JOB, "jd": None, "position": "Analyste principal"}))

    def test_short_jd_is_not_blank(self):
        self.assertIsNone(core.local_result({**JOB, "jd": "Analyst."}))


class TestFailureClassification(unittest.TestCase):
    def test_auth_failure_stops_the_whole_run(self):
        with self.assertRaises(core.GlobalFatalError):
            core.classify_cli_failure({"is_error": True, "result": "Failed", "api_error_status": 401}, "")
        with self.assertRaises(core.GlobalFatalError):
            core.classify_cli_failure({"is_error": True, "result": "Not logged in · Please run /login"}, "")

    def test_bad_cli_arguments_stop_the_whole_run(self):
        with self.assertRaises(core.GlobalFatalError):
            core.classify_cli_failure(None, "error: unknown option '--system-prompt-fil'")

    def test_prompt_too_long_is_a_job_failure_without_retry(self):
        with self.assertRaises(core.JobFatalError) as cm:
            core.classify_cli_failure({"is_error": True, "result": "Prompt is too long"}, "")
        self.assertFalse(cm.exception.retryable)

    def test_unknown_errors_are_left_to_the_retry_policy(self):
        self.assertIsNone(core.classify_cli_failure({"is_error": True, "result": "Overloaded"}, ""))


class TestDuplicateDetection(unittest.TestCase):
    def rec(self, platform, key, position, decision, jd):
        return ({"platform": platform, "source_key": key, "position": position, "company": "Acme",
                 "apply_url": key, "decision": decision}, jd)

    def test_same_job_on_two_sources_keeps_the_accept_as_primary(self):
        merged = [self.rec("greenhouse", "g1", "Data Analyst", "REJECT", JD),
                  self.rec("ashby", "a1", "Data Analyst (Toronto)", "ACCEPT", JD + " Apply now.")]
        (group,) = merge.mark_duplicates(merged)
        self.assertEqual(group[0]["source_key"], "a1")
        self.assertEqual(merged[0][0]["duplicate_of"], {"platform": "ashby", "source_key": "a1"})

    def test_template_jds_with_different_levels_are_not_merged(self):
        merged = [self.rec("greenhouse", "g1", "Senior AI Engineer", "REJECT", JD),
                  self.rec("greenhouse", "g2", "Lead AI Engineer", "REJECT", JD)]
        self.assertEqual(merge.mark_duplicates(merged), [])

    def test_same_title_different_jd_is_not_merged(self):
        other = "Completely different responsibilities about warehouse forklifts and shipping docks " * 3
        merged = [self.rec("greenhouse", "g1", "Data Analyst", "ACCEPT", JD),
                  self.rec("ashby", "a1", "Data Analyst", "ACCEPT", other)]
        self.assertEqual(merge.mark_duplicates(merged), [])


if __name__ == "__main__":
    unittest.main()
