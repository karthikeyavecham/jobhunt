"""Arbeitsagentur adapter, against saved real responses."""

import json
import unittest
from pathlib import Path

from jobhunt.sources.arbeitsagentur import (ArbeitsagenturAdapter, _company_of,
                                            _date_of, _employment_of, _location_of)
from jobhunt.sources.base import SourceError, markdown_to_text

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    with open(str(FIXTURES / name), encoding="utf-8") as handle:
        return json.load(handle)


class TestSearchParsing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load("arbeitsagentur_search.json")

    def test_every_result_parses(self):
        parsed = [ArbeitsagenturAdapter._parse(i) for i in self.payload["ergebnisliste"]]
        self.assertTrue(all(job is not None for job in parsed))

    def test_builds_the_public_job_url(self):
        job = ArbeitsagenturAdapter._parse(self.payload["ergebnisliste"][0])
        self.assertTrue(
            job.url.startswith("https://www.arbeitsagentur.de/jobsuche/jobdetail/")
        )
        self.assertIn(job.external_id, job.url)

    def test_search_results_carry_no_description(self):
        # The description only arrives via fetch_detail, so this must be empty
        # or the "is it worth a detail call" logic would be wrong.
        job = ArbeitsagenturAdapter._parse(self.payload["ergebnisliste"][0])
        self.assertIsNone(job.description_text)

    def test_missing_reference_number_is_skipped(self):
        self.assertIsNone(ArbeitsagenturAdapter._parse({"stellenangebotsTitel": "X"}))
        self.assertIsNone(ArbeitsagenturAdapter._parse({"referenznummer": "1"}))

    def test_fetch_without_terms_explains_itself(self):
        with self.assertRaises(SourceError) as ctx:
            ArbeitsagenturAdapter().fetch({})
        self.assertIn("search terms", str(ctx.exception))


class TestFieldMapping(unittest.TestCase):
    def test_company_prefers_firma(self):
        self.assertEqual(_company_of({"firma": "GLS Germany"}), "GLS Germany")

    def test_company_falls_back_to_alliance_partner(self):
        self.assertEqual(
            _company_of({"allianzpartnerName": "GLS Karriere"}), "GLS Karriere"
        )

    def test_anonymous_listings_stay_distinct(self):
        """Chiffre listings have no employer; two must not merge into one."""
        a = _company_of({"chiffrenummer": "4805"})
        b = _company_of({"chiffrenummer": "9912"})
        self.assertNotEqual(a, b)
        self.assertIn("4805", a)

    def test_location_combines_postcode_and_city(self):
        item = {"stellenlokationen": [{"adresse": {"plz": "53332", "ort": "Bornheim"}}]}
        self.assertEqual(_location_of(item), "53332 Bornheim")

    def test_location_absent_is_none(self):
        self.assertIsNone(_location_of({}))
        self.assertIsNone(_location_of({"stellenlokationen": []}))

    def test_employment_type_describes_hours_and_duration(self):
        result = _employment_of({"arbeitszeitVollzeit": True, "vertragsdauer": "UNBEFRISTET"})
        self.assertIn("Vollzeit", result)
        self.assertIn("unbefristet", result)

    def test_employment_type_empty_is_none(self):
        self.assertIsNone(_employment_of({}))

    def test_date_uses_first_publication(self):
        self.assertEqual(
            _date_of({"datumErsteVeroeffentlichung": "2026-06-10"}),
            "2026-06-10T00:00:00+00:00",
        )

    def test_date_falls_back_to_publication_window(self):
        self.assertEqual(
            _date_of({"veroeffentlichungszeitraum": {"von": "2026-06-01"}}),
            "2026-06-01T00:00:00+00:00",
        )

    def test_date_absent_is_none(self):
        self.assertIsNone(_date_of({}))


class TestMarkdownDescription(unittest.TestCase):
    def test_real_detail_body_becomes_readable_text(self):
        detail = load("arbeitsagentur_detail.json")
        text = markdown_to_text(detail["stellenangebotsBeschreibung"])
        self.assertTrue(text)
        self.assertNotIn("**", text)
        self.assertNotIn("&amp;", text)
        # Content survived the flattening.
        self.assertIn("Logistik", text)

    def test_entities_are_unescaped(self):
        self.assertEqual(markdown_to_text("Produktion &amp; Logistik"), "Produktion & Logistik")

    def test_bullets_become_visible_markers(self):
        self.assertIn("•", markdown_to_text("- first\n- second"))

    def test_links_keep_their_text(self):
        self.assertEqual(markdown_to_text("[Apply here](https://x.example)"), "Apply here")

    def test_empty_is_safe(self):
        self.assertEqual(markdown_to_text(None), "")
        self.assertEqual(markdown_to_text(""), "")


class RecordingSession(object):
    """Captures the query parameters instead of calling the API."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {"ergebnisliste": [], "maxErgebnisse": 0}

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        payload = self.payload
        return type("R", (), {
            "status_code": 200,
            "json": lambda self: payload,
        })()


class TestSearchScope(unittest.TestCase):
    """Nationwide search, added 2026-08-13.

    A Köln-centred radius cannot return a remote role advertised in Berlin,
    however wide it is set — the geographic filter has to come off entirely.
    """

    def run_fetch(self, config):
        adapter = ArbeitsagenturAdapter()
        session = RecordingSession()
        adapter._session_cache = session
        adapter.fetch(dict(config, terms=["Zendesk"]))
        return session.calls[0]

    def test_a_blank_location_drops_the_geographic_filter(self):
        params = self.run_fetch({"wo": ""})
        self.assertNotIn("wo", params)
        self.assertNotIn("umkreis", params)

    def test_whitespace_counts_as_blank(self):
        """An empty box on a config page rarely submits a truly empty string."""
        params = self.run_fetch({"wo": "   "})
        self.assertNotIn("wo", params)

    def test_a_location_still_sends_wo_and_umkreis(self):
        params = self.run_fetch({"wo": "Köln", "umkreis": 50})
        self.assertEqual(params["wo"], "Köln")
        self.assertEqual(params["umkreis"], 50)

    def test_the_default_is_still_the_commutable_region(self):
        """Existing sources carry no explicit `wo`; they must not go national."""
        params = self.run_fetch({})
        self.assertEqual(params["wo"], "Köln")

    def test_offer_type_is_sent_when_configured(self):
        """angebotsart=1 is salaried employment — keeps apprenticeships out."""
        self.assertEqual(self.run_fetch({"angebotsart": 1})["angebotsart"], 1)

    def test_offer_type_is_omitted_when_not_configured(self):
        self.assertNotIn("angebotsart", self.run_fetch({}))

    def test_the_search_window_is_configurable(self):
        self.assertEqual(self.run_fetch({"veroeffentlichtseit": 7})
                         ["veroeffentlichtseit"], 7)


if __name__ == "__main__":
    unittest.main()
