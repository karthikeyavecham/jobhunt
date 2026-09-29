"""Adapter parsing, against a saved real response so tests stay offline."""

import json
import unittest
from pathlib import Path

from jobhunt.sources.arbeitnow import ArbeitnowAdapter, _epoch_to_iso
from jobhunt.sources.base import html_to_text

FIXTURE = Path(__file__).parent / "fixtures" / "arbeitnow_page1.json"


class TestArbeitnowParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(str(FIXTURE), encoding="utf-8") as handle:
            cls.payload = json.load(handle)

    def test_every_fixture_row_parses(self):
        parsed = [ArbeitnowAdapter._parse(item) for item in self.payload["data"]]
        self.assertTrue(all(job is not None for job in parsed))
        self.assertEqual(len(parsed), len(self.payload["data"]))

    def test_maps_the_documented_fields(self):
        item = self.payload["data"][0]
        job = ArbeitnowAdapter._parse(item)
        self.assertEqual(job.external_id, item["slug"])
        self.assertEqual(job.title, item["title"])
        self.assertEqual(job.company, item["company_name"])
        self.assertEqual(job.url, item["url"])

    def test_description_text_is_derived_from_html(self):
        job = ArbeitnowAdapter._parse(self.payload["data"][0])
        self.assertTrue(job.description_text)
        self.assertNotIn("<p>", job.description_text)

    def test_incomplete_row_is_skipped_not_stored_half(self):
        self.assertIsNone(ArbeitnowAdapter._parse({"slug": "x", "url": "u"}))
        self.assertIsNone(ArbeitnowAdapter._parse({}))

    def test_remote_flag_becomes_boolean(self):
        job = ArbeitnowAdapter._parse(self.payload["data"][0])
        self.assertIn(job.remote, (True, False))


class TestEpochConversion(unittest.TestCase):
    def test_converts_unix_timestamp(self):
        self.assertTrue(_epoch_to_iso(1785958234).startswith("2026-"))

    def test_accepts_numeric_string(self):
        self.assertEqual(_epoch_to_iso("1785958234"), _epoch_to_iso(1785958234))

    def test_bad_input_returns_none(self):
        for value in [None, "", "not-a-number", {}]:
            self.assertIsNone(_epoch_to_iso(value))


class TestHtmlToText(unittest.TestCase):
    def test_bullets_survive_as_separate_lines(self):
        text = html_to_text("<ul><li>Zendesk admin</li><li>REST APIs</li></ul>")
        self.assertIn("Zendesk admin", text)
        self.assertIn("REST APIs", text)
        self.assertNotEqual(text, "Zendesk adminREST APIs")

    def test_scripts_are_dropped(self):
        self.assertNotIn("alert", html_to_text("<p>Hi</p><script>alert(1)</script>"))

    def test_empty_is_safe(self):
        self.assertEqual(html_to_text(None), "")
        self.assertEqual(html_to_text(""), "")


if __name__ == "__main__":
    unittest.main()
