import copy
import importlib.util
import os
import sys
import tempfile
import unittest

from tests.helpers import JOB, REPO, needs_numpy, setup


def production_v1():
    """The pipeline's own V1 validator, loaded read-only, to check the rendered text against it."""
    os.environ.setdefault("LJP_WORKSPACE", tempfile.mkdtemp(prefix="ljp_rag_"))
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("v1_for_rag_test", REPO / "resume" / "script" / "02_generate_v1.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # dataclasses in the module look themselves up here
    spec.loader.exec_module(mod)
    return mod


@needs_numpy
class ContractVerifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fact_rag import contract, fake_model
        cls.contract, cls.fake = contract, fake_model
        cls.s = setup("hashing")
        cls.r, cls.rag = cls.s.context(JOB, "rag")
        cls.clean = fake_model.generate(cls.s.snapshot, cls.rag, JOB, cls.s.lexicon, cls.r)

    def check(self, obj, bundle=None):
        return self.s.check(obj, bundle or self.rag)["verification"]

    def row(self, ver, claim_id):
        return next(r for r in ver["claims"] if r["claim_id"] == claim_id)

    def test_clean_output_passes_and_renders_in_the_production_layout(self):
        res = self.s.check(self.clean, self.rag)
        self.assertEqual(res["verification"]["summary"]["fail_auto"], 0)
        self.assertEqual(res["verification"]["summary"]["citation_valid_rate"], 1.0)
        v1 = production_v1()
        self.assertIsNone(v1.validate_resume(res["resume_text"], JOB["position"]))
        self.assertTrue(res["resume_text"].startswith("Morgan Ellis\n"))

    def test_schema_rejects_wrong_shape(self):
        bad = copy.deepcopy(self.clean)
        bad["experience"]["EXP-FAB"].pop()
        with self.assertRaises(self.contract.ContractError):
            self.contract.validate(bad, self.s.experience_ids)
        with self.assertRaises(self.contract.ContractError):
            self.contract.parse_output("Here you go: {}", self.s.experience_ids)

    def test_every_planted_defect_fails(self):
        flawed, planted = self.fake.inject_errors(self.clean, self.s.snapshot, self.rag)
        ver = self.check(flawed)
        for p in planted:
            self.assertEqual(self.row(ver, p["claim_id"])["status"], "FAIL_AUTO", p)
        codes = ver["summary"]["flag_codes"]
        for code in ("NUMBER_NOT_IN_CORPUS", "TOOL_NOT_IN_CORPUS", "CITATION_WRONG_ENTITY",
                     "CITATION_NOT_IN_CONTEXT", "LIMITATION_FRAMEWORK_ONLY"):
            self.assertIn(code, codes)

    def test_rag_citation_outside_the_context_fails_but_full_does_not(self):
        _, full = self.s.context(JOB, "full")
        missing = next(f["fact_id"] for f in self.s.snapshot["facts"] if f["fact_type"] == "canonical_bullet"
                       and f["entity_id"] == "EXP-FAB" and f["fact_id"] not in set(self.rag["provided_fact_ids"]))
        obj = copy.deepcopy(self.clean)
        obj["experience"]["EXP-FAB"][0] = {"text": self.s.snapshot["facts"][0]["text"], "fact_ids": [missing]}
        self.assertIn("CITATION_NOT_IN_CONTEXT",
                      [f["code"] for f in self.row(self.check(obj), "EXP-FAB-1")["flags"]])
        self.assertNotIn("CITATION_NOT_IN_CONTEXT",
                         [f["code"] for f in self.row(self.check(obj, full), "EXP-FAB-1")["flags"]])

    def test_unit_change_and_uncited_claims(self):
        obj = copy.deepcopy(self.clean)
        fab = {"text": "Lifted recall at fixed precision by 12% over the baseline with XGBoost.",
               "fact_ids": ["FAB-B03"]}
        _, full = self.s.context(JOB, "full")
        obj["experience"]["EXP-FAB"][0] = fab
        obj["qualifications"][0]["fact_ids"] = []
        ver = self.check(obj, full)
        self.assertIn("NUMBER_UNIT_CHANGED", [f["code"] for f in self.row(ver, "EXP-FAB-1")["flags"]])
        q = self.row(ver, "QUAL1")
        self.assertEqual(q["status"], "UNKNOWN")  # never counted as a pass
        self.assertFalse(q["citations_valid"])

    def test_vocabulary_only_skill_is_unknown_not_supported(self):
        obj = copy.deepcopy(self.clean)
        obj["skills"]["professional"] = [{"name": "Stakeholder Engagement", "fact_ids": ["SKL-CORE-PROFESSIONAL-SKILLS"]}]
        _, full = self.s.context(JOB, "full")
        self.assertEqual(self.row(self.check(obj, full), "SKP1")["status"], "UNKNOWN")

    def test_provenance_records_source_and_hash(self):
        prov = self.s.check(self.clean, self.rag)["provenance"]
        self.assertEqual(prov["context_mode"], "rag")
        self.assertEqual(prov["corpus_hash"], self.s.snapshot["corpus_hash"])
        src = prov["claims"][0]["sources"][0]
        self.assertTrue(src["known"] and src["file"].endswith(".md") and len(src["content_hash"]) == 16)


@needs_numpy
class PromptTest(unittest.TestCase):
    def test_full_prompt_prefix_is_job_independent_and_rag_differs(self):
        from fact_rag import prompts
        s = setup("hashing")
        other = dict(JOB, position="Data Analyst", jd="We need SQL and Tableau dashboards for retail sales.")
        out = {}
        for mode in ("full", "rag"):
            out[mode] = [prompts.system_prompt("Morgan Ellis", s.context(j, mode)[1], s.experience_ids)[0]
                         for j in (JOB, other)]
        self.assertEqual(out["full"][0], out["full"][1])
        self.assertNotEqual(out["rag"][0], out["rag"][1])
        self.assertIn("EXP-NWF", out["rag"][0])
        self.assertNotIn(JOB["jd"][:40], out["full"][0])  # the JD only ever goes in the user prompt


if __name__ == "__main__":
    unittest.main()
