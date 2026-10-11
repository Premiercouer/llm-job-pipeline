import importlib.util
import pathlib
import unittest
from http.client import IncompleteRead
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).resolve().parents[1] / "01_filter_greenhouse_boards.py"
SPEC = importlib.util.spec_from_file_location("greenhouse_filter", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class LocationMatchingTests(unittest.TestCase):
    def assert_match(self, *locations):
        for location in locations:
            with self.subTest(location=location):
                self.assertTrue(MODULE.match_canadian_location(location)[0])

    def assert_no_match(self, *locations):
        for location in locations:
            with self.subTest(location=location):
                self.assertFalse(MODULE.match_canadian_location(location)[0])

    def test_requested_examples(self):
        self.assert_match(
            "Toronto, Ontario, Canada", "Toronto, ON", "Toronto",
            "Remote - Canada", "Canada - Remote", "Ontario, Canada",
            "Mississauga, ON, Canada", "Toronto / Remote",
        )

    def test_other_canadian_locations(self):
        self.assert_match(
            "Vancouver, BC", "Remote - AB", "Montréal, Québec",
            "Halifax, Nova Scotia", "ON", "Greater Toronto Area",
            "Vancouver, BC OR Chicago, IL",
        )

    def test_countryless_remote_is_not_assumed_canadian(self):
        self.assert_no_match(
            "Remote", "Remote - US", "Anywhere", "Work on site",
            "Burlington, Massachusetts, United States",
            "London, England, United Kingdom",
            "London, UK", "Cambridge, MA", "Vancouver, WA",
            "Waterloo, IA", "Victoria, TX", "Yukon, OK",
            "Ontario, CA", "London", "London - More London", "NL- Remote",
            "Milton, New Hampshire, United States",
        )

    def test_explicit_canada_wins_for_multi_country_location(self):
        self.assert_match(
            "Remote - Canada or United States",
            "Ontario, CAN; Remote-Friendly, United States",
            "Chicago, Atlanta, US-Remote, Toronto",
        )

    def test_foreign_country_wins_over_ambiguous_region_code_or_name(self):
        self.assert_no_match(
            "Recife / PE / Brazil",
            "Ontario, California, United States",
            "New Brunswick, New Jersey, United States",
        )


class FetchRetryTests(unittest.TestCase):
    def test_incomplete_read_is_retried(self):
        with mock.patch.object(
            MODULE, "http_get",
            side_effect=[IncompleteRead(b"partial", 10), (200, b'{"jobs": []}', {})],
        ) as http_get, mock.patch.object(MODULE.time, "sleep"):
            result = MODULE.fetch_json_endpoint("https://example.test", 1, 3, 0)
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["request_attempts"], 2)
        self.assertEqual(http_get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
