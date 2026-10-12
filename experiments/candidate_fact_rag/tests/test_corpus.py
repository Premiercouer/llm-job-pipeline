import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import EXP, REPO

from fact_rag import corpus, quant

CANDIDATE = REPO / "examples" / "candidate"
OVERLAY, LEXICON = EXP / "config" / "limitations.json", EXP / "config" / "tool_lexicon.json"


def build(cdir=CANDIDATE):
    return corpus.build(cdir, OVERLAY, LEXICON)


class CorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snap = build()
        cls.facts = corpus.facts_by_id(cls.snap)

    def test_existing_bullet_ids_are_kept(self):
        ids = [f["fact_id"] for f in self.snap["facts"] if f["fact_type"] == "canonical_bullet"]
        self.assertEqual(len(ids), 10)
        self.assertIn("FAB-B01", ids)
        self.assertTrue(all(self.facts[i]["id_origin"] == "existing" for i in ids))
        self.assertEqual(corpus.experience_ids(self.snap), ["EXP-FAB", "EXP-NWF"])

    def test_project_facts_are_complete_units_with_derived_ids(self):
        self.assertEqual(self.facts["PRJ02-TECH"]["fact_type"], "tech_stack")
        self.assertIn("SageMaker", self.facts["PRJ02-TECH"]["text"])
        self.assertTrue(self.facts["PRJ01-HDR"]["text"].startswith("Retail Demand Forecasting | Timeline:"))
        ach = [f for f in self.snap["facts"] if f["entity_id"] == "PRJ01" and f["fact_type"] == "achievement"]
        self.assertEqual([f["fact_id"] for f in ach], ["PRJ01-ACH01", "PRJ01-ACH02"])

    def test_ids_and_hash_are_stable_across_builds(self):
        again = build()
        self.assertEqual(again["corpus_hash"], self.snap["corpus_hash"])
        self.assertEqual([f["fact_id"] for f in again["facts"]], [f["fact_id"] for f in self.snap["facts"]])
        corpus.verify_snapshot(again)

    def test_source_lines_point_at_the_fact(self):
        for fid in ("FAB-B03", "NWF-B05", "PRJ01-QR", "PRJ02-ACH02"):
            f = self.facts[fid]
            lines = (CANDIDATE / "knowledge" / f["source"]["file"]).read_text().splitlines()
            span = "\n".join(lines[f["source"]["line_start"] - 1: f["source"]["line_end"]])
            self.assertIn(f["text"].split(",")[0][:30].replace("**", ""), span.replace("**", ""), fid)
            self.assertEqual(f["content_hash"], corpus.sha256_text(f["text"]))

    def test_editing_one_fact_changes_only_its_hash_and_the_corpus_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            cdir = Path(tmp) / "candidate"
            shutil.copytree(CANDIDATE, cdir)
            bank = cdir / "knowledge" / "fabrikam_health_bullet_bank.md"
            bank.write_text(bank.read_text().replace("120M claims records", "125M claims records"))
            other = build(cdir)
        self.assertNotEqual(other["corpus_hash"], self.snap["corpus_hash"])
        changed = [f["fact_id"] for f, g in zip(self.snap["facts"], other["facts"]) if f["content_hash"] != g["content_hash"]]
        self.assertEqual(changed, ["FAB-B01"])

    def test_vocabulary_and_tags_are_not_evidence(self):
        vocab = [f for f in self.snap["facts"] if f["entity_id"] == "SKILLS"]
        self.assertTrue(vocab and not any(f["is_evidence"] for f in vocab))
        ats = [f for f in vocab if f["fact_type"] == "skill_vocab_ats"]
        self.assertEqual(len(ats), 5)  # '- **Category**: terms' list format
        self.assertIn("Spark SQL", ats[0]["terms"])
        self.assertFalse(self.facts["PRJ01-KW"]["is_evidence"])

    def test_limitations_follow_the_entity(self):
        self.assertEqual(self.facts["PRJ04-ACH"]["implementation_status"], "framework_design_only")
        self.assertEqual([l["code"] for l in self.facts["PRJ04-ACH"]["limitations"]], ["FRAMEWORK_ONLY"])
        self.assertEqual([l["code"] for l in self.facts["PRJ02-QR"]["limitations"]], ["COURSE_PROJECT"])
        self.assertEqual(self.facts["FAB-B01"]["limitations"], [])

    def test_numbers_keep_units_apart(self):
        pp = quant.extract("lifting recall by 12 percentage points")[0]
        pct = quant.extract("cutting duplicates by 31%")[0]
        self.assertEqual((pp["value"], pp["unit_class"]), (12.0, "pp"))
        self.assertEqual((pct["value"], pct["unit_class"]), (31.0, "percent"))
        self.assertEqual(quant.extract("Processed 120M records")[0]["value"], 120e6)
        self.assertEqual(quant.extract("FAB-B01 in Sep 2025"), [])


if __name__ == "__main__":
    unittest.main()
