"""Dedup rules. If these leak, Slack fills with duplicates and the app dies."""

import unittest

from jobhunt import dedup


class TestNormalizeCompany(unittest.TestCase):
    def test_strips_legal_forms(self):
        for variant in ["Acme SE", "Acme SE.", "acme se", "Acme"]:
            self.assertEqual(dedup.normalize_company(variant), "acme")

    def test_strips_compound_german_legal_form(self):
        self.assertEqual(
            dedup.normalize_company("Cinesupply GmbH & Co. KG"),
            dedup.normalize_company("Cinesupply"),
        )

    def test_folds_umlauts_consistently(self):
        self.assertEqual(
            dedup.normalize_company("Müller GmbH"), dedup.normalize_company("Mueller GmbH")
        )

    def test_keeps_group_and_holding(self):
        # These distinguish real entities rather than decorating one.
        self.assertNotEqual(
            dedup.normalize_company("DHL Group"), dedup.normalize_company("DHL")
        )

    def test_empty_is_safe(self):
        self.assertEqual(dedup.normalize_company(None), "")
        self.assertEqual(dedup.normalize_company(""), "")


class TestNormalizeTitle(unittest.TestCase):
    def test_strips_gender_markers(self):
        base = dedup.normalize_title("Support Operations Manager")
        for variant in [
            "Support Operations Manager (m/w/d)",
            "Support Operations Manager (w/m/d)",
            "Support Operations Manager (m/f/d)",
            "Support Operations Manager (f/m/x)",
            "Support Operations Manager (all genders)",
            "Support Operations Manager (gn)",
            "Support Operations Manager m/w/d",
        ]:
            self.assertEqual(dedup.normalize_title(variant), base, variant)

    def test_preserves_seniority(self):
        self.assertNotEqual(
            dedup.normalize_title("Senior Support Manager"),
            dedup.normalize_title("Support Manager"),
        )

    def test_does_not_eat_meaningful_brackets(self):
        # "(Remote)" is not a gender marker and must survive as content.
        self.assertIn("remote", dedup.normalize_title("Support Manager (Remote)"))

    def test_real_arbeitnow_title(self):
        self.assertEqual(
            dedup.normalize_title("Senior Motion Designer:in (m/w/d)"),
            "senior motion designer in",
        )


class TestNormalizeCity(unittest.TestCase):
    def test_folds_koeln_spellings(self):
        """Both spellings appear in live Arbeitnow data for the target city."""
        expected = "koeln"
        for variant in ["Köln", "koeln", "Koln", "Cologne", "50667 Köln",
                        "Köln, Germany", "Köln, Nordrhein-Westfalen", "KÖLN",
                        "Remote / Köln", "Cologne, Germany"]:
            self.assertEqual(dedup.normalize_city(variant), expected, variant)

    def test_folds_duesseldorf_spellings(self):
        expected = "duesseldorf"
        for variant in ["Düsseldorf", "Dusseldorf", "duesseldorf",
                        "Remote / Düsseldorf", "40213 Düsseldorf"]:
            self.assertEqual(dedup.normalize_city(variant), expected, variant)

    def test_folds_muenchen_spellings(self):
        expected = "muenchen"
        for variant in ["München", "Munich", "Munchen", "muenchen"]:
            self.assertEqual(dedup.normalize_city(variant), expected, variant)

    def test_work_mode_words_are_not_mistaken_for_cities(self):
        self.assertEqual(dedup.normalize_city("Remote"), "")
        self.assertEqual(dedup.normalize_city("Home Office"), "")

    def test_frankfurt_variants_are_left_distinct(self):
        # Collapsing these would merge Frankfurt am Main with Frankfurt (Oder).
        self.assertNotEqual(
            dedup.normalize_city("Frankfurt am Main"), dedup.normalize_city("Frankfurt (Oder)")
        )

    def test_empty_is_safe(self):
        self.assertEqual(dedup.normalize_city(None), "")


class TestDedupKey(unittest.TestCase):
    def test_same_role_across_sources_collides(self):
        a = dedup.dedup_key("FREE NOW GmbH", "Zendesk Platform Owner (m/w/d)", "50667 Köln")
        b = dedup.dedup_key("FREE NOW", "Zendesk Platform Owner", "Koeln, Germany")
        self.assertEqual(a, b)

    def test_different_city_does_not_collide(self):
        a = dedup.dedup_key("FREE NOW", "Zendesk Platform Owner", "Köln")
        b = dedup.dedup_key("FREE NOW", "Zendesk Platform Owner", "Berlin")
        self.assertNotEqual(a, b)

    def test_different_role_does_not_collide(self):
        a = dedup.dedup_key("FREE NOW", "Zendesk Platform Owner", "Köln")
        b = dedup.dedup_key("FREE NOW", "Support Operations Manager", "Köln")
        self.assertNotEqual(a, b)


class TestCanonicalUrl(unittest.TestCase):
    def test_strips_tracking_params(self):
        self.assertEqual(
            dedup.canonical_url("https://Example.com/jobs/42?utm_source=linkedin&gh_src=abc"),
            "https://example.com/jobs/42",
        )

    def test_keeps_meaningful_params(self):
        self.assertEqual(
            dedup.canonical_url("https://example.com/jobs?id=42&utm_medium=email"),
            "https://example.com/jobs?id=42",
        )

    def test_drops_fragment_and_trailing_slash(self):
        self.assertEqual(
            dedup.canonical_url("https://example.com/jobs/42/#apply"),
            "https://example.com/jobs/42",
        )

    def test_empty_is_safe(self):
        self.assertEqual(dedup.canonical_url(None), "")


class TestContentHash(unittest.TestCase):
    def test_whitespace_and_case_insensitive(self):
        self.assertEqual(
            dedup.content_hash("Zendesk   admin\n\nrequired"),
            dedup.content_hash("zendesk admin required"),
        )

    def test_different_text_differs(self):
        self.assertNotEqual(dedup.content_hash("a"), dedup.content_hash("b"))


if __name__ == "__main__":
    unittest.main()
