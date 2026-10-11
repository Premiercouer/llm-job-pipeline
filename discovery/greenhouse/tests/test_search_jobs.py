import importlib.util
import json
import pathlib
import unittest
from datetime import datetime, timezone
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "01_search_jobs.py"
SPEC = importlib.util.spec_from_file_location("greenhouse_search", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class LocationTests(unittest.TestCase):
    def test_pass(self):
        values = (
            "Toronto", "Toronto, ON", "Toronto, Ontario, Canada",
            "Mississauga, ON", "Markham, Ontario", "Greater Toronto Area",
            "Remote - Canada", "Remote (Canada)", "Toronto / New York",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertTrue(MODULE.match_target_location(value)[0])

    def test_fail(self):
        values = (
            "Vancouver, BC", "Montreal, QC", "Calgary, AB", "Remote",
            "Remote - US", "Anywhere", "New York", "San Francisco",
            "Ontario, California",
        )
        for value in values:
            with self.subTest(value=value):
                self.assertFalse(MODULE.match_target_location(value)[0])


class UpdatedAtTests(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)

    def test_recent(self):
        result = MODULE.classify_updated_at("2026-10-04T12:00:00Z", now=self.NOW)
        self.assertEqual(result["status"], "valid")
        self.assertTrue(result["updated_within_7d"])

    def test_old_missing_and_invalid_reject(self):
        for value, status in (
            ("2026-09-01T12:00:00+00:00", "valid"),
            (None, "missing"), ("", "missing"), ("not-a-date", "invalid"),
        ):
            with self.subTest(value=value):
                result = MODULE.classify_updated_at(value, now=self.NOW)
                self.assertEqual(result["status"], status)
                self.assertFalse(result["updated_within_7d"])


class DiscoveryStateTests(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)

    @staticmethod
    def job(job_id, title="Data Analyst", location="Toronto", updated="2026-10-05T00:00:00Z"):
        return {
            "id": job_id, "title": title, "location": {"name": location},
            "updated_at": updated, "absolute_url": f"https://example.test/{job_id}",
        }

    def test_all_discovered_ids_are_pending_but_not_persisted(self):
        rows = [{
            "board_token": "example", "status": "LIVE",
            "jobs": [
                self.job(1), self.job(2, location="Vancouver, BC"),
                self.job(3, updated="2020-01-01T00:00:00Z"), self.job(4, title=""),
            ],
        }]
        state = {"version": 1, "updated_at": self.NOW.isoformat(), "seen": {}}
        candidates, pending, counts = MODULE.collect_unseen_cheap_candidates(rows, state, self.NOW)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(pending), 4)
        self.assertEqual(state["seen"], {})
        self.assertEqual(counts["newly_seen"], 4)
        self.assertEqual(counts["location_rejected"], 1)
        self.assertEqual(counts["freshness_rejected"], 1)
        self.assertEqual(counts["invalid_jobs"], 1)

    def test_ten_thousand_jobs_do_not_save_during_discovery(self):
        rows = [{
            "board_token": "bulk", "status": "LIVE",
            "jobs": [self.job(index) for index in range(10_000)],
        }]
        state = {"version": 1, "updated_at": self.NOW.isoformat(), "seen": {}}
        with mock.patch.object(MODULE, "save_seen_state") as save:
            candidates, pending, counts = MODULE.collect_unseen_cheap_candidates(rows, state, self.NOW)
        self.assertEqual(len(candidates), 10_000)
        self.assertEqual(len(pending), 10_000)
        self.assertEqual(counts["newly_seen"], 10_000)
        save.assert_not_called()

    def test_title_is_not_locally_filtered(self):
        for title in ("Financial Analyst", "Project Analyst", "ETL Tester", "Anything"):
            with self.subTest(title=title):
                candidate, outcome = MODULE.build_cheap_candidate("example", self.job(1, title=title), self.NOW)
                self.assertEqual(outcome, "candidate")
                self.assertEqual(candidate["position"], title)


class TitleScreenContractTests(unittest.TestCase):
    IDS = ["greenhouse:example:1", "greenhouse:example:2", "greenhouse:example:3"]

    def test_complete_partition(self):
        accepted, rejected = MODULE.validate_screening_results({
            "accepted_ids": self.IDS[:2], "rejected_ids": self.IDS[2:],
        }, self.IDS)
        self.assertEqual(accepted + rejected, self.IDS)

    def test_missing_duplicate_unknown_and_overlap_rejected(self):
        malformed = (
            {"accepted_ids": self.IDS[:1], "rejected_ids": self.IDS[2:]},
            {"accepted_ids": [self.IDS[0], self.IDS[0]], "rejected_ids": self.IDS[1:]},
            {"accepted_ids": self.IDS + ["greenhouse:other:9"], "rejected_ids": []},
            {"accepted_ids": self.IDS, "rejected_ids": [self.IDS[0]]},
            {"accepted_ids": self.IDS, "rejected_ids": [], "reasons": {}},
        )
        for document in malformed:
            with self.subTest(document=document), self.assertRaises(RuntimeError):
                MODULE.validate_screening_results(document, self.IDS)


class CandidateContractTests(unittest.TestCase):
    def test_candidate_document_needs_no_reason(self):
        job = {
            "source_key": "greenhouse:example:1", "platform": "greenhouse",
            "board_token": "example", "job_id": 1, "position": "Data Analyst",
            "location": "Toronto", "updated_at": "2026-10-05T00:00:00Z",
            "updated_within_7d": True, "absolute_url": "https://example.test/1",
        }
        MODULE.validate_candidate_document({"candidate_count": 1, "jobs": [job]})
        with self.assertRaises(RuntimeError):
            MODULE.validate_candidate_document({"candidate_count": 2, "jobs": [job, dict(job)]})


if __name__ == "__main__":
    unittest.main()
