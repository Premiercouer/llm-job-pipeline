import importlib.util
import pathlib
import unittest
from datetime import datetime, timezone


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "02_fetch_jd.py"
SPEC = importlib.util.spec_from_file_location("greenhouse_detail", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class TimestampTests(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 0, 0, tzinfo=timezone.utc)

    def test_recent_z(self):
        result = MODULE.classify_first_published("2026-10-04T12:00:00Z", self.NOW)
        self.assertTrue(result["published_within_7d"])

    def test_old(self):
        result = MODULE.classify_first_published("2026-09-01T00:00:00+00:00", self.NOW)
        self.assertFalse(result["published_within_7d"])

    def test_missing_and_invalid(self):
        for value, status in ((None, "missing"), ("", "missing"), ("bad", "invalid")):
            with self.subTest(value=value):
                result = MODULE.classify_first_published(value, self.NOW)
                self.assertEqual(result["status"], status)
                self.assertFalse(result["published_within_7d"])

    def test_timezone_offset(self):
        result = MODULE.classify_first_published("2026-09-30T20:30:00-04:00", self.NOW)
        self.assertTrue(result["published_within_7d"])


class DeadlineTests(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)

    def test_future_and_past(self):
        self.assertFalse(MODULE.classify_deadline("2026-10-07", self.NOW)["application_deadline_passed"])
        self.assertTrue(MODULE.classify_deadline("2026-10-05", self.NOW)["application_deadline_passed"])

    def test_null_and_invalid(self):
        self.assertIsNone(MODULE.classify_deadline(None, self.NOW)["application_deadline_passed"])
        self.assertEqual(MODULE.classify_deadline("bad", self.NOW)["status"], "invalid")


class CleaningTests(unittest.TestCase):
    def test_html_and_entities(self):
        value = (
            "<p>Hello &amp; world</p><ul><li>SQL</li><li>Python</li></ul>"
            "<br>&nbsp;&lt;p&gt;Encoded paragraph&lt;/p&gt;"
            "&amp;lt;p&amp;gt;Double encoded&amp;lt;/p&amp;gt;"
        )
        cleaned = MODULE.clean_job_description(value)
        self.assertIn("Hello & world", cleaned)
        self.assertIn("- SQL", cleaned)
        self.assertIn("- Python", cleaned)
        self.assertIn("Encoded paragraph", cleaned)
        self.assertIn("Double encoded", cleaned)
        self.assertNotIn("<p>", cleaned)
        self.assertNotIn("&amp;", cleaned)

    def test_paragraphs_do_not_stick(self):
        self.assertEqual(MODULE.clean_job_description("<p>Hello</p><p>World</p>"), "Hello\n\nWorld")


class DetailValidationTests(unittest.TestCase):
    def setUp(self):
        self.candidate = {"job_id": 123}
        self.detail = {
            "id": 123, "title": "Data Analyst", "company_name": "Example",
            "location": {"name": "Toronto"}, "absolute_url": "https://example.test/job",
            "content": "<p>Hello</p>", "first_published": "2026-10-05T00:00:00Z",
        }

    def test_matching_id(self):
        self.assertEqual(MODULE.validate_detail(self.detail, self.candidate)["status"], "VALID")

    def test_mismatched_id(self):
        detail = dict(self.detail, id=999)
        self.assertEqual(MODULE.validate_detail(detail, self.candidate)["status"], "DETAIL_ID_MISMATCH")

    def test_missing_company(self):
        detail = dict(self.detail, company_name=None)
        result = MODULE.validate_detail(detail, self.candidate)
        self.assertEqual(result["status"], "INVALID_DETAIL")
        self.assertEqual(result["company_status"], "missing")

    def test_missing_content(self):
        detail = dict(self.detail, content=None)
        self.assertEqual(MODULE.validate_detail(detail, self.candidate)["status"], "EMPTY_JD")

    def test_missing_first_published(self):
        detail = dict(self.detail, first_published=None)
        self.assertEqual(MODULE.validate_detail(detail, self.candidate)["status"], "PUBLISHED_DATE_UNKNOWN")

    def test_missing_absolute_url(self):
        detail = dict(self.detail, absolute_url=None)
        self.assertEqual(MODULE.validate_detail(detail, self.candidate)["status"], "INVALID_DETAIL")

    def test_title_never_filters_valid_recent_detail(self):
        candidate = {
            "source_key": "greenhouse:example:123", "board_token": "example", "job_id": 123,
            "position": "placeholder", "location": "Toronto", "updated_at": "2026-10-05T00:00:00Z",
            "updated_within_7d": True, "absolute_url": "https://example.test/job",
        }
        original = MODULE.CORE.fetch_json_endpoint
        try:
            for title in ("Financial Analyst", "ETL Tester", "Project Analyst", "Data Analyst"):
                with self.subTest(title=title):
                    detail = dict(self.detail, title=title)
                    MODULE.CORE.fetch_json_endpoint = lambda *args, d=detail: {
                        "status": "SUCCESS", "data": d, "request_attempts": 1,
                    }
                    result = MODULE.process_candidate(candidate, datetime(2026, 10, 6, tzinfo=timezone.utc), 1, 1, 0)
                    self.assertEqual(result["kind"], "job")
        finally:
            MODULE.CORE.fetch_json_endpoint = original


class BackendContractTests(unittest.TestCase):
    NOW = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.rich = {
            "internal_source_key": "greenhouse:example:123",
            "board_token": "example", "job_id": 123,
            "company": "Example Company", "position": "Data Analyst",
            "jd": "A complete description", "apply_url": "https://example.test/jobs/123",
        }

    def test_valid_serialization_is_exact(self):
        job = MODULE.serialize_backend_job(self.rich)
        self.assertEqual(set(job), MODULE.BACKEND_JOB_KEYS)
        self.assertEqual(job["source_key"], job["apply_url"])

    def test_reject_missing_or_empty_fields(self):
        for field, value in (("company", None), ("position", ""), ("jd", " ")):
            with self.subTest(field=field):
                rich = dict(self.rich, **{field: value})
                with self.assertRaises(RuntimeError):
                    MODULE.serialize_backend_job(rich)

    def test_reject_invalid_apply_url(self):
        for value in (None, "", "ftp://example.test/job", "not-a-url"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    MODULE.serialize_backend_job(dict(self.rich, apply_url=value))

    def test_top_level_exact_schema_and_count(self):
        document, duplicates = MODULE.build_backend_document([self.rich], self.NOW)
        self.assertEqual(set(document), MODULE.BACKEND_TOP_LEVEL_KEYS)
        self.assertEqual(document["date"], "2026-10-05")
        self.assertEqual(document["job_count"], len(document["jobs"]))
        self.assertFalse(duplicates)

    def test_validator_rejects_duplicate_source_key(self):
        job = MODULE.serialize_backend_job(self.rich)
        document = {"date": "2026-10-05", "job_count": 2, "jobs": [job, dict(job)]}
        with self.assertRaises(RuntimeError):
            MODULE.validate_backend_document(document)

    def test_builder_deduplicates_and_audits(self):
        duplicate = dict(self.rich, internal_source_key="greenhouse:other:456", board_token="other", job_id=456)
        document, duplicates = MODULE.build_backend_document([self.rich, duplicate], self.NOW)
        self.assertEqual(document["job_count"], 1)
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["status"], "DUPLICATE_BACKEND_SOURCE_KEY")

    def test_reject_extra_top_level_or_job_fields(self):
        job = MODULE.serialize_backend_job(self.rich)
        with self.assertRaises(RuntimeError):
            MODULE.validate_backend_document({"date": "2026-10-05", "job_count": 1, "jobs": [job], "extra": 1})
        bad_job = dict(job, location="Toronto")
        with self.assertRaises(RuntimeError):
            MODULE.validate_backend_document({"date": "2026-10-05", "job_count": 1, "jobs": [bad_job]})


if __name__ == "__main__":
    unittest.main()
