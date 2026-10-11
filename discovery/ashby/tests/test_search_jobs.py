import importlib.util
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "01_search_jobs.py"
SPEC = importlib.util.spec_from_file_location("ashby_search", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class Stage01Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = staticmethod(
            MODULE.STAGE00.load_greenhouse_matcher(MODULE.STAGE00.DEFAULT_GREENHOUSE_RULE)
        )
        cls.now = datetime(2026, 10, 6, tzinfo=timezone.utc)

    def job(self, **updates):
        value = {
            "id": "abc", "title": "Data Analyst", "isListed": True,
            "location": "Toronto, Ontario, Canada", "secondaryLocations": [],
            "address": {"postalAddress": {"addressCountry": "Canada", "addressLocality": "Toronto"}},
            "isRemote": False, "workplaceType": "OnSite",
            "publishedAt": "2026-10-05T12:00:00+00:00",
            "applyUrl": "https://jobs.ashbyhq.com/example/abc/application",
            "descriptionPlain": "Complete job description.\nSecond line.",
        }
        value.update(updates)
        return value

    def test_candidate_uses_identity_and_plain_description_unchanged(self):
        raw = self.job()
        candidate, outcome, error = MODULE.build_candidate(
            {"company": "Example", "board_slug": "example"}, raw, self.matcher, self.now
        )
        self.assertEqual((outcome, error), ("candidate", None))
        self.assertEqual(candidate["source_key"], "ashby:example:abc")
        self.assertEqual(candidate["jd"], raw["descriptionPlain"])

    def test_filter_order_outcomes(self):
        board = {"company": "Example", "board_slug": "example"}
        cases = [
            (self.job(isListed=False), "is_listed_reject"),
            (self.job(location="New York", address={"postalAddress": {"addressCountry": "United States"}}), "location_reject"),
            (self.job(publishedAt="2026-09-01T00:00:00Z"), "freshness_reject"),
        ]
        for job, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(MODULE.build_candidate(board, job, self.matcher, self.now)[1], expected)

    def test_seen_and_duplicate_identity(self):
        board = {"company": "Example", "board_slug": "example", "ok": True, "jobs": [self.job(), self.job()]}
        state = {"version": 1, "updated_at": self.now.isoformat(), "seen": {}}
        candidates, pending, failures, counts = MODULE.discover([board], state, self.matcher, self.now, None)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(counts["identity_duplicates"], 1)
        self.assertEqual(set(pending), {"ashby:example:abc"})
        self.assertFalse(failures)

    def test_invalid_candidate_is_not_marked_seen(self):
        board = {"company": "Example", "board_slug": "example", "ok": True, "jobs": [self.job(descriptionPlain="")]}
        state = {"version": 1, "updated_at": self.now.isoformat(), "seen": {}}
        _, pending, failures, counts = MODULE.discover([board], state, self.matcher, self.now, None)
        self.assertEqual(pending, {})
        self.assertEqual(counts["invalid_jobs"], 1)
        self.assertEqual(len(failures), 1)

    def test_title_cap_defers_without_seen_commit(self):
        jobs = [self.job(id=str(i), applyUrl=f"https://example.test/{i}") for i in range(3)]
        board = {"company": "Example", "board_slug": "example", "ok": True, "jobs": jobs}
        state = {"version": 1, "updated_at": self.now.isoformat(), "seen": {}}
        candidates, pending, _, counts = MODULE.discover([board], state, self.matcher, self.now, 2)
        self.assertEqual(len(candidates), 2)
        self.assertEqual(len(pending), 2)
        self.assertEqual(counts["title_screen_deferred"], 1)

    def test_title_partition_and_backend_schema(self):
        ids = ["ashby:a:1", "ashby:a:2"]
        accepted, rejected = MODULE.validate_title_results(
            {"accepted_ids": [ids[0]], "rejected_ids": [ids[1]]}, ids
        )
        self.assertEqual((accepted, rejected), ([ids[0]], [ids[1]]))
        document = {"date": "2026-10-06", "job_count": 1, "jobs": [{
            "company": "Example", "position": "Analyst", "jd": "Full JD",
            "apply_url": "https://example.test/apply", "source_key": "https://example.test/apply",
        }]}
        MODULE.validate_backend_document(document)

    def test_atomic_seen_round_trip(self):
        state = {"version": 1, "updated_at": self.now.isoformat(), "seen": {
            "ashby:example:abc": {"first_seen_at": self.now.isoformat()}
        }}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "seen.json"
            MODULE.save_seen_state(path, state)
            self.assertEqual(json.loads(path.read_text()), state)

    def test_missing_seen_state_has_stable_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "missing.json"
            first = MODULE.load_seen_state(path, "2026-10-06T00:00:00+00:00")
            second = MODULE.load_seen_state(path, "2026-10-07T00:00:00+00:00")
        self.assertEqual(MODULE.fingerprint(first), MODULE.fingerprint(second))

    def test_only_404_is_a_terminal_fetch_failure(self):
        self.assertTrue(MODULE.FetchCheckpoint.terminal({"ok": False, "error_type": "HTTP_404"}))
        self.assertFalse(MODULE.FetchCheckpoint.terminal({"ok": False, "error_type": "HTTP_503"}))
        self.assertFalse(MODULE.FetchCheckpoint.terminal({"ok": False, "error_type": "TRANSPORT_ERROR"}))


if __name__ == "__main__":
    unittest.main()
