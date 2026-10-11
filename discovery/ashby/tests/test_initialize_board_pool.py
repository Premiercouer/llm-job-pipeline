import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "00_initialize_board_pool.py"
SPEC = importlib.util.spec_from_file_location("ashby_initialize", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "nested" / "value.json"
            MODULE.atomic_write_json(path, {"ok": True})
            self.assertEqual(json.loads(path.read_text()), {"ok": True})
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class MasterImportTests(unittest.TestCase):
    def test_filters_and_case_insensitively_deduplicates_preserving_first_case(self):
        csv_body = (
            "ats_vendor,company_name,board_slug,last_crawled\n"
            "ashby,One,OneBoard,2026-01-01\n"
            "ASHBY,Duplicate,oneboard,2026-01-02\n"
            "greenhouse,Other,other,2026-01-01\n"
        ).encode()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            MODULE, "http_get", return_value=(200, csv_body, {})
        ):
            path = pathlib.Path(directory) / "master.json"
            result = MODULE.import_master("https://example.test/source.csv", path, 1)
        self.assertEqual(result["board_count"], 1)
        self.assertEqual(result["boards"][0]["board_slug"], "OneBoard")


class LocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = staticmethod(MODULE.load_greenhouse_matcher(MODULE.DEFAULT_GREENHOUSE_RULE))

    def evaluate(self, **overrides):
        job = {
            "id": "1", "title": "Example", "isListed": True,
            "location": "New York", "secondaryLocations": [],
            "address": {"postalAddress": {"addressCountry": "United States"}},
            "isRemote": False, "workplaceType": "OnSite",
        }
        job.update(overrides)
        return MODULE.evaluate_job_location(job, self.matcher)

    def test_toronto_primary(self):
        result = self.evaluate(
            location="Toronto, Ontario",
            address={"postalAddress": {"addressLocality": "Toronto", "addressCountry": "Canada"}},
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["category"], "toronto")

    def test_gta_secondary(self):
        result = self.evaluate(secondaryLocations=[{
            "location": "Mississauga, ON",
            "address": {"postalAddress": {"addressLocality": "Mississauga", "addressCountry": "Canada"}},
        }])
        self.assertTrue(result["matched"])
        self.assertEqual(result["category"], "gta")

    def test_canada_remote_requires_explicit_canada(self):
        matched = self.evaluate(
            location="Canada", address={"postalAddress": {"addressCountry": "Canada"}},
            isRemote=True, workplaceType="Remote",
        )
        self.assertTrue(matched["matched"])
        self.assertEqual(matched["category"], "canada_remote")
        ambiguous = self.evaluate(
            location="Remote", address={"postalAddress": {}},
            isRemote=True, workplaceType="Remote",
        )
        self.assertFalse(ambiguous["matched"])
        self.assertTrue(ambiguous["indeterminate"])
        self.assertEqual(ambiguous["reason"], "remote_without_explicit_canada_evidence")

    def test_remote_us_only_is_definite_non_target(self):
        for location in ("Remote - United States", "US Remote", "Remote, USA"):
            with self.subTest(location=location):
                result = self.evaluate(
                    location=location,
                    address={"postalAddress": {"addressCountry": "United States"}},
                    isRemote=True, workplaceType="Remote",
                )
                self.assertFalse(result["matched"])
                self.assertFalse(result["indeterminate"])
                self.assertEqual(result["reason"], "definite_non_target_location")

    def test_remote_uk_and_india_are_definite_non_target(self):
        for location, country in (("Remote - UK", "United Kingdom"), ("Remote - India", "India")):
            with self.subTest(location=location):
                result = self.evaluate(
                    location=location, address={"postalAddress": {"addressCountry": country}},
                    isRemote=True, workplaceType="Remote",
                )
                self.assertFalse(result["indeterminate"])
                self.assertEqual(result["reason"], "definite_non_target_location")

    def test_remote_canada_is_target(self):
        result = self.evaluate(
            location="Remote - Canada",
            address={"postalAddress": {"addressCountry": "Canada"}},
            isRemote=True, workplaceType="Remote",
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["category"], "canada_remote")

    def test_worldwide_and_north_america_stay_indeterminate(self):
        for location in ("Remote - Worldwide", "Global", "North America", "Americas"):
            with self.subTest(location=location):
                result = self.evaluate(
                    location=location, address={"postalAddress": {}},
                    isRemote=True, workplaceType="Remote",
                )
                self.assertFalse(result["matched"])
                self.assertTrue(result["indeterminate"])

    def test_is_remote_with_us_office_is_not_assumed_us_only(self):
        result = self.evaluate(
            location="San Francisco",
            address={"postalAddress": {
                "addressLocality": "San Francisco", "addressRegion": "California",
                "addressCountry": "United States",
            }},
            isRemote=True, workplaceType="Hybrid",
        )
        self.assertFalse(result["matched"])
        self.assertTrue(result["indeterminate"])

    def test_toronto_primary_wins_over_us_secondary(self):
        result = self.evaluate(
            location="Toronto, Ontario, Canada",
            address={"postalAddress": {"addressLocality": "Toronto", "addressCountry": "Canada"}},
            secondaryLocations=[{
                "location": "Remote - United States",
                "address": {"postalAddress": {"addressCountry": "United States"}},
            }],
            isRemote=True,
        )
        self.assertTrue(result["matched"])
        self.assertEqual(result["category"], "toronto")

    def test_burlington_vermont_is_not_gta(self):
        result = self.evaluate(
            location="Burlington, VT",
            address={"postalAddress": {
                "addressLocality": "Burlington", "addressRegion": "Vermont",
                "addressCountry": "United States",
            }},
            isRemote=True,
        )
        self.assertFalse(result["matched"])

    def test_indeterminate_diagnostic_retains_raw_fields(self):
        job = {
            "id": "raw-1", "title": "Raw", "isListed": True,
            "location": "Worldwide", "secondaryLocations": [],
            "address": {"postalAddress": {}}, "isRemote": True, "workplaceType": "Remote",
        }
        with mock.patch.object(MODULE, "fetch_board", return_value={
            "ok": True, "http_status": 200, "jobs": [job], "request_attempts": 1,
            "request_seconds": 0.1, "request_url": "https://example.test",
        }):
            result = MODULE.scan_board(
                {"company": "Example", "board_slug": "example", "source": "test"},
                self.matcher, 1, 0, 0,
            )
        diagnostic = result["indeterminate_jobs"][0]
        for key in (
            "title", "location", "secondaryLocations", "address", "isRemote",
            "workplaceType", "evaluation_contexts", "evaluation_reason",
        ):
            self.assertIn(key, diagnostic)

    def test_unlisted_is_not_a_valid_match(self):
        result = self.evaluate(location="Toronto", isListed=False)
        self.assertFalse(result["matched"])
        self.assertFalse(result["indeterminate"])


class FetchTests(unittest.TestCase):
    def test_404_is_not_retried(self):
        with mock.patch.object(MODULE, "http_get", return_value=(404, b"", {})) as get:
            result = MODULE.fetch_board("missing", 1, 3, 0)
        self.assertEqual(result["error_type"], "HTTP_404")
        self.assertEqual(get.call_count, 1)

    def test_429_and_5xx_are_retried(self):
        payload = json.dumps({"jobs": []}).encode()
        with mock.patch.object(
            MODULE, "http_get", side_effect=[(429, b"", {}), (503, b"", {}), (200, payload, {})]
        ) as get, mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.fetch_board("example", 1, 3, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["request_attempts"], 3)
        self.assertEqual(get.call_count, 3)


class CheckpointTests(unittest.TestCase):
    def test_completed_results_resume_but_transport_errors_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = pathlib.Path(directory)
            master = directory_path / "master.json"
            master.write_text("{}")
            path = directory_path / "checkpoint.json"
            checkpoint = MODULE.Checkpoint(path, master)
            checkpoint.put({"board_slug": "Good", "status": "IN_POOL"})
            checkpoint.put({"board_slug": "Retry", "status": "TRANSPORT_ERROR"})
            resumed = MODULE.Checkpoint(path, master)
            self.assertTrue(resumed.completed("good"))
            self.assertFalse(resumed.completed("retry"))


if __name__ == "__main__":
    unittest.main()
