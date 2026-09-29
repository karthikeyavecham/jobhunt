"""Stage-0 relevance filtering.

Arbeitnow returns Europe-wide postings; on live data 72 of 270 were UK roles.
These tests pin down that the region rules keep what is commutable or remotely
reachable and drop the rest — without dropping the target region by accident.
"""

import unittest

from jobhunt import filters
from jobhunt.sources.base import RawJob


def job(location=None, remote=False, company="Some GmbH", posted_at=None):
    return RawJob(
        external_id="x", url="https://e.example/x", title="Support Manager",
        company=company, location=location, remote=remote, posted_at=posted_at,
    )


class TestCommutable(unittest.TestCase):
    def test_target_region_passes(self):
        for city in ["Köln", "50667 Köln", "Leverkusen", "Düsseldorf", "Bonn",
                     "Bergisch Gladbach", "Langenfeld (Rheinland)", "Erkrath"]:
            ok, reason = filters.check_location(city, remote=False)
            self.assertTrue(ok, "{0} should be commutable".format(city))
            self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_english_city_names_are_recognised(self):
        """Regression: 'Cologne' was excluded because only 'Köln' matched.

        Arbeitnow writes English exonyms, so without alias resolution his
        primary target city would be filtered out.
        """
        ok, reason = filters.check_location("Cologne", remote=False)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_ascii_umlaut_spelling_is_recognised(self):
        ok, reason = filters.check_location("Dusseldorf", remote=False)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_commutable_wins_over_a_country_suffix(self):
        ok, reason = filters.check_location("Köln, Deutschland", remote=False)
        self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_remote_prefix_does_not_hide_the_city(self):
        ok, reason = filters.check_location("Remote / Düsseldorf", remote=True)
        self.assertEqual(reason, filters.PASS_COMMUTABLE)


class TestElsewhereInGermany(unittest.TestCase):
    def test_onsite_elsewhere_is_excluded(self):
        for city in ["Berlin", "München", "Hamburg", "Stuttgart"]:
            ok, reason = filters.check_location(city, remote=False)
            self.assertFalse(ok, city)
            self.assertEqual(reason, filters.FAIL_TOO_FAR)

    def test_remote_elsewhere_is_kept(self):
        ok, reason = filters.check_location("Berlin", remote=True)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_REMOTE_DE)

    def test_english_city_name_elsewhere_is_recognised(self):
        """'Munich' must resolve like 'München', not fall through as unknown."""
        ok, reason = filters.check_location("Munich", remote=False)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_TOO_FAR)

    def test_company_prefixed_city_still_resolves(self):
        # Live Arbeitnow data contains values like "Mindfuel Munich".
        ok, reason = filters.check_location("Mindfuel Munich", remote=False)
        self.assertEqual(reason, filters.FAIL_TOO_FAR)


class TestRelocation(unittest.TestCase):
    """`allow_relocation`, added 2026-08-13 when the nationwide source landed.

    Off by default: relocating is a bigger decision than commuting, and having
    it on by accident would bury the commutable roles.
    """

    RELOCATING = {"allow_relocation": True}

    def test_off_by_default(self):
        ok, reason = filters.check_location("Berlin", remote=False)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_TOO_FAR)

    def test_onsite_elsewhere_passes_when_relocating(self):
        for city in ["Berlin", "München", "Hamburg", "Dresden"]:
            ok, reason = filters.check_location(city, remote=False,
                                                config=self.RELOCATING)
            self.assertTrue(ok, city)
            self.assertEqual(reason, filters.PASS_RELOCATION)

    def test_commutable_is_still_reported_as_commutable(self):
        """The reason has to stay honest, or the audit view loses its meaning."""
        ok, reason = filters.check_location("Köln", remote=False,
                                            config=self.RELOCATING)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_remote_elsewhere_is_still_reported_as_remote(self):
        ok, reason = filters.check_location("Berlin", remote=True,
                                            config=self.RELOCATING)
        self.assertEqual(reason, filters.PASS_REMOTE_DE)

    def test_foreign_is_still_excluded(self):
        """Relocating within Germany is the point — not to London."""
        for city in ["London", "Amsterdam", "Paris", "Dublin"]:
            ok, reason = filters.check_location(city, remote=False,
                                                config=self.RELOCATING)
            self.assertFalse(ok, city)
            self.assertEqual(reason, filters.FAIL_FOREIGN)

    def test_an_unrecognised_town_still_needs_to_be_remote(self):
        """Not on the foreign list is not evidence of being in Germany.

        Arbeitnow returns Europe-wide results, so letting unknown locations
        through here would quietly admit Kraków and Tallinn.
        """
        ok, reason = filters.check_location("Nowheresville", remote=False,
                                            config=self.RELOCATING)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_TOO_FAR)

    def test_a_missing_location_still_needs_to_be_remote(self):
        ok, reason = filters.check_location("", remote=False,
                                            config=self.RELOCATING)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_NO_LOCATION)

    def test_a_generic_germany_marker_passes_when_relocating(self):
        ok, reason = filters.check_location("Deutschland", remote=False,
                                            config=self.RELOCATING)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_RELOCATION)


class TestForeign(unittest.TestCase):
    def test_uk_is_excluded_even_when_remote(self):
        """A UK remote role generally cannot be filled from Germany."""
        for place in ["London", "London, United Kingdom", "Cheltenham", "Chatham"]:
            ok, reason = filters.check_location(place, remote=True)
            self.assertFalse(ok, place)
            self.assertEqual(reason, filters.FAIL_FOREIGN)

    def test_other_foreign_markers(self):
        for place in ["Amsterdam", "Paris", "Dublin", "Zurich", "Vienna", "New York"]:
            ok, reason = filters.check_location(place, remote=True)
            self.assertFalse(ok, place)


class TestUnknownAndRemote(unittest.TestCase):
    def test_bare_remote_is_kept(self):
        ok, reason = filters.check_location("Remote", remote=True)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_REMOTE_UNKNOWN)

    def test_remote_job_noise_word_is_not_a_city(self):
        ok, reason = filters.check_location("Remote job", remote=True)
        self.assertTrue(ok)
        self.assertEqual(reason, filters.PASS_REMOTE_UNKNOWN)

    def test_no_location_and_not_remote_is_excluded(self):
        ok, reason = filters.check_location("", remote=False)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_NO_LOCATION)

    def test_unrecognised_town_onsite_is_excluded_but_findable(self):
        ok, reason = filters.check_location("Kleinkleckersdorf", remote=False)
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_TOO_FAR)


class TestEvaluate(unittest.TestCase):
    def test_company_blocklist(self):
        ok, reason = filters.evaluate(
            job(location="Köln", company="Bad Corp"),
            {"company_blocklist": ["bad corp"]},
        )
        self.assertFalse(ok)
        self.assertIn("blocklist", reason)

    def test_age_cutoff(self):
        ok, reason = filters.evaluate(
            job(location="Köln", posted_at="2020-01-01T00:00:00+00:00"),
            {"max_age_days": 30},
        )
        self.assertFalse(ok)
        self.assertEqual(reason, filters.FAIL_TOO_OLD)

    def test_age_ignored_when_unset(self):
        ok, _ = filters.evaluate(job(location="Köln", posted_at="2020-01-01T00:00:00+00:00"))
        self.assertTrue(ok)

    def test_accepts_a_dict_row(self):
        ok, reason = filters.evaluate(
            {"location": "Köln", "remote": 0, "company": "X", "posted_at": None}
        )
        self.assertEqual(reason, filters.PASS_COMMUTABLE)

    def test_allow_remote_can_be_turned_off(self):
        ok, reason = filters.evaluate(
            job(location="Berlin", remote=True), {"allow_remote": False}
        )
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
