import copy
import unittest

from tests.helpers import JOB, needs_numpy, needs_sklearn, setup


@needs_numpy
class RetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fact_rag import evidence, jd, retrieval
        cls.jd, cls.retrieval, cls.evidence = jd, retrieval, evidence
        cls.s = setup("hashing")
        cls.r = retrieval.retrieve_for_job(cls.s.index, JOB, cls.s.cfg)

    def test_boilerplate_sections_are_dropped(self):
        dropped = {d["heading"] for d in self.r["dropped_jd_sections"]}
        self.assertTrue({"About Us", "What We Offer", "Equal Opportunity"} <= dropped)
        texts = " ".join(q["text"] for q in self.r["queries"]).lower()
        self.assertNotIn("dental", texts)
        self.assertNotIn("accommodation", texts)

    def test_short_skill_names_become_queries(self):
        skills = {q["text"] for q in self.r["queries"] if q["type"] == "skill"}
        self.assertTrue({"SQL", "Python", "pandas"} <= skills)

    def test_experience_quotas_and_project_mandatory_facts(self):
        sel = set(self.r["selected_fact_ids"])
        q = self.s.cfg["selection"]["experience_quota"]
        for eid in self.s.experience_ids:
            n = sum(c["selected"] for c in self.r["candidates"] if c["entity_id"] == eid)
            self.assertGreaterEqual(n, q["min"])
            self.assertLessEqual(n, q["max"])
        self.assertEqual(len(self.r["chosen_projects"]), self.s.cfg["selection"]["n_projects"])
        for p in self.r["chosen_projects"]:
            self.assertIn(f"{p}-HDR", sel)
        for c in self.r["candidates"]:
            self.assertTrue(c["reason"], c["fact_id"])  # every candidate has a recorded decision
            if c["entity_type"] == "project" and c["entity_id"] not in self.r["chosen_projects"]:
                self.assertFalse(c["selected"])

    def test_budget_drops_lowest_non_mandatory_first(self):
        cfg = copy.deepcopy(self.s.cfg)
        cfg["selection"]["evidence_token_budget"] = 900
        r = self.retrieval.retrieve_for_job(self.s.index, JOB, cfg)
        self.assertTrue(r["dropped_for_budget"])
        by = {c["fact_id"]: c for c in r["candidates"]}
        self.assertFalse(any(by[i]["mandatory"] for i in r["dropped_for_budget"]))
        kept = [c["agg"] for c in r["candidates"] if c["selected"] and not c["mandatory"]]
        if kept:
            self.assertLessEqual(max(by[i]["agg"] for i in r["dropped_for_budget"]), min(kept) + 1e-9)

    def test_insufficient_bundle_is_flagged(self):
        cfg = copy.deepcopy(self.s.cfg)
        cfg["selection"].update(n_projects=0, evidence_token_budget=50)
        r = self.retrieval.retrieve_for_job(self.s.index, JOB, cfg)
        self.assertEqual(r["status"], "RETRIEVAL_INSUFFICIENT")
        self.assertIn("no_project", r["insufficient_reasons"])
        self.assertIn("over_budget_after_mandatory", r["insufficient_reasons"])

    def test_rag_bundle_is_a_subset_rendered_like_full(self):
        full = self.evidence.bundle(self.s.snapshot, "full")
        rag = self.evidence.bundle(self.s.snapshot, "rag", self.r)
        self.assertLess(set(rag["provided_fact_ids"]), set(full["provided_fact_ids"]))
        self.assertLess(rag["token_estimate"], full["token_estimate"])
        for fid in self.r["selected_fact_ids"]:  # same line in both modes
            line = next(l for l in rag["text"].splitlines() if l.startswith(f"[{fid}]"))
            self.assertIn(line, full["text"])
        self.assertIn("NOT experience evidence", rag["text"])
        self.assertIn("LIMITATION [", full["text"])

    def test_retrieval_is_deterministic(self):
        again = self.retrieval.retrieve_for_job(self.s.index, JOB, self.s.cfg)
        self.assertEqual(again["selected_fact_ids"], self.r["selected_fact_ids"])
        self.assertEqual(again["retrieval_config_hash"], self.r["retrieval_config_hash"])


@needs_sklearn
class LsaTest(unittest.TestCase):
    def test_tfidf_lsa_backend_retrieves_relevant_project(self):
        from fact_rag import retrieval
        s = setup("tfidf_lsa")
        r = retrieval.retrieve_for_job(s.index, JOB, s.cfg)
        self.assertEqual(s.index.embedder.model_id, "tfidf_lsa-128-seed13")
        self.assertIn("PRJ01", r["chosen_projects"])  # the forecasting project for a JD that asks for forecasting
        self.assertNotIn("PRJ02", r["chosen_projects"])
        self.assertEqual(r["status"], "OK")

    def test_readme_trace_example_still_holds(self):
        """The 'one claim, traced end to end' table in the README quotes these values."""
        from fact_rag import fake_model
        s = setup("tfidf_lsa")
        r, rag = s.context(JOB, "rag")
        c = next(c for c in r["candidates"] if c["fact_id"] == "NWF-B05")
        best = next(q for q in r["queries"] if q["qid"] == c["best_query"])
        self.assertTrue(c["selected"])
        self.assertTrue(best["text"].startswith("Pull shipment events from carrier REST APIs"))
        self.assertEqual((round(c["best_bm25_norm"], 2), round(c["best_dense"], 2), round(c["agg"], 3)),
                         (1.0, 0.95, 1.198))
        self.assertEqual((len(rag["provided_fact_ids"]), rag["context_hash"][:12], rag["corpus_hash"][:12]),
                         (21, "64992e5dcc5a", "3261fd63796d"))
        res = s.check(fake_model.generate(s.snapshot, rag, JOB, s.lexicon, r), rag)
        prov = next(x for x in res["provenance"]["claims"] if x["claim_id"] == "EXP-NWF-3")
        ver = next(x for x in res["verification"]["claims"] if x["claim_id"] == "EXP-NWF-3")
        self.assertEqual(prov["fact_ids"], ["NWF-B05"])
        self.assertEqual((prov["sources"][0]["lines"], prov["sources"][0]["content_hash"]), ([29, 33], "1b7fc717aea850c4"))
        self.assertEqual((ver["status"], ver["flags"]), ("SUPPORTED_AUTO", []))


if __name__ == "__main__":
    unittest.main()
