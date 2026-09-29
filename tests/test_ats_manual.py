"""ATS adapters and manual import.

Adapter parsing runs against saved shapes rather than the network. The manual
import tests concentrate on the guards — an SSRF hole or a silently-wrong slug
is the kind of thing that stays broken for months.
"""

import tempfile
import unittest
from pathlib import Path

from jobhunt import db as db_module
from jobhunt import ingest
from jobhunt.sources import ats, manual
from jobhunt.sources.base import SourceError


class TestSlugValidation(unittest.TestCase):
    def setUp(self):
        self.adapter = ats.GreenhouseAdapter()

    def test_missing_slug_explains_what_a_slug_is(self):
        with self.assertRaises(SourceError) as ctx:
            self.adapter.fetch({})
        self.assertIn("slug", str(ctx.exception).lower())

    def test_injection_in_a_slug_is_rejected(self):
        for bad in ["../../etc", "a b", "x/../y", "gitlab?admin=1", ""]:
            with self.assertRaises(SourceError):
                self.adapter.fetch({"slug": bad})

    def test_ordinary_slugs_pass_validation(self):
        for good in ["gitlab", "scheidt-bachmann", "free_now", "acme.gmbh"]:
            self.assertEqual(self.adapter._slug({"slug": good}), good)

    def test_surrounding_slashes_are_trimmed(self):
        self.assertEqual(self.adapter._slug({"slug": "/gitlab/"}), "gitlab")


class TestGreenhouseParsing(unittest.TestCase):
    ITEM = {
        "id": 8503792002,
        "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/8503792002",
        "title": "  Account Executive - Italy  ",
        "company_name": "GitLab",
        "location": {"name": "Remote, Italy"},
        "first_published": "2026-04-17T05:58:03-04:00",
        # Greenhouse double-escapes the body.
        "content": "&lt;p&gt;Own the &amp;amp; platform&lt;/p&gt;",
    }

    def test_fields_map_across(self):
        job = ats.GreenhouseAdapter()._parse(self.ITEM, {"slug": "gitlab"})
        self.assertEqual(job.external_id, "8503792002")
        self.assertEqual(job.title, "Account Executive - Italy")
        self.assertEqual(job.company, "GitLab")
        self.assertEqual(job.location, "Remote, Italy")
        self.assertTrue(job.posted_at.startswith("2026-04-17"))

    def test_escaped_html_body_is_unescaped_to_text(self):
        job = ats.GreenhouseAdapter()._parse(self.ITEM, {"slug": "gitlab"})
        self.assertIn("Own the", job.description_text)
        self.assertNotIn("&lt;", job.description_text)

    def test_incomplete_item_is_skipped(self):
        self.assertIsNone(ats.GreenhouseAdapter()._parse({"id": 1}, {"slug": "x"}))

    def test_configured_company_overrides_the_feed(self):
        job = ats.GreenhouseAdapter()._parse(
            self.ITEM, {"slug": "gitlab", "company": "GitLab Inc."})
        self.assertEqual(job.company, "GitLab Inc.")


class TestLeverParsing(unittest.TestCase):
    ITEM = {
        "id": "abc-123",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "text": "Support Operations Manager",
        "categories": {"location": "Berlin", "commitment": "Full-time"},
        "workplaceType": "hybrid",
        "createdAt": 1553186035299,
        "descriptionPlain": "Own the support desk.",
        "lists": [{"text": "Requirements", "content": "<li>Zendesk</li>"}],
        "additionalPlain": "Great benefits.",
    }

    def test_fields_map_across(self):
        job = ats.LeverAdapter()._parse(self.ITEM, {"slug": "acme"})
        self.assertEqual(job.title, "Support Operations Manager")
        self.assertEqual(job.location, "Berlin")
        self.assertTrue(job.remote)          # hybrid counts

    def test_requirement_lists_are_folded_into_the_description(self):
        """Lever keeps requirements outside the description body."""
        job = ats.LeverAdapter()._parse(self.ITEM, {"slug": "acme"})
        self.assertIn("Own the support desk", job.description_text)
        self.assertIn("Requirements", job.description_text)
        self.assertIn("Zendesk", job.description_text)

    def test_epoch_milliseconds_are_converted(self):
        job = ats.LeverAdapter()._parse(self.ITEM, {"slug": "acme"})
        self.assertTrue(job.posted_at.startswith("2019-"))


class TestDateNormalisation(unittest.TestCase):
    def test_epoch_milliseconds(self):
        self.assertTrue(ats._iso(1553186035299).startswith("2019-"))

    def test_iso_with_z(self):
        self.assertTrue(ats._iso("2026-07-14T08:29:20.852Z").startswith("2026-07-14"))

    def test_iso_with_offset(self):
        self.assertIsNotNone(ats._iso("2026-04-17T05:58:03-04:00"))

    def test_junk_returns_none(self):
        for value in (None, "", "not-a-date", {}):
            self.assertIsNone(ats._iso(value))


class TestManualFetchGuards(unittest.TestCase):
    """These guards are the reason manual import is safe to expose in a form."""

    def test_sites_that_forbid_scraping_are_refused(self):
        for url in ["https://www.linkedin.com/jobs/view/1",
                    "https://www.stepstone.de/x",
                    "https://de.indeed.com/viewjob?jk=1",
                    "https://www.glassdoor.de/job/1"]:
            self.assertFalse(manual.can_fetch(url), url)

    def test_ordinary_sites_are_allowed(self):
        for url in ["https://www.arbeitnow.com/jobs/1",
                    "https://boards.greenhouse.io/gitlab/jobs/1",
                    "https://careers.deepl.com/x"]:
            self.assertTrue(manual.can_fetch(url), url)

    def test_the_refusal_explains_the_workaround(self):
        reason = manual.blocked_host_reason("https://www.linkedin.com/jobs/view/1")
        self.assertIn("paste", reason.lower())

    def test_non_http_schemes_are_rejected(self):
        for url in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x"]:
            with self.assertRaises(SourceError):
                manual.fetch_url(url)

    def test_loopback_is_rejected(self):
        """Without this the form could be used to read the app's own pages."""
        with self.assertRaises(SourceError) as ctx:
            manual.fetch_url("http://127.0.0.1:5002/config")
        self.assertIn("private address", str(ctx.exception))

    def test_private_lan_addresses_are_rejected(self):
        with self.assertRaises(SourceError):
            manual.fetch_url("http://10.0.0.44:1234/")


class StubProvider(object):
    def __init__(self, result):
        self.result = result

    def complete_json(self, system, user, schema, max_tokens=1500, temperature=0.1):
        self.last_user = user
        return dict(self.result)


def extraction(**overrides):
    base = {
        "title": "Zendesk Platform Owner", "company": "FREE NOW",
        "location": "Hamburg", "employment_type": "Full-time", "remote": False,
        "description": "Own the Zendesk estate end to end.", "is_job_posting": True,
    }
    base.update(overrides)
    return base


class TestManualExtraction(unittest.TestCase):
    LONG = "Zendesk Platform Owner at FREE NOW. " * 10

    def test_short_input_is_rejected_before_calling_the_model(self):
        with self.assertRaises(SourceError):
            manual.extract("too short", StubProvider(extraction()))

    def test_non_posting_is_rejected_with_a_useful_message(self):
        provider = StubProvider(extraction(is_job_posting=False))
        with self.assertRaises(SourceError) as ctx:
            manual.extract(self.LONG, provider)
        self.assertIn("login wall", str(ctx.exception))

    def test_missing_title_is_rejected(self):
        with self.assertRaises(SourceError):
            manual.extract(self.LONG, StubProvider(extraction(title="  ")))

    def test_valid_posting_is_accepted(self):
        fields = manual.extract(self.LONG, StubProvider(extraction()))
        self.assertEqual(fields["company"], "FREE NOW")

    def test_the_pasted_text_is_labelled_as_data(self):
        provider = StubProvider(extraction())
        manual.extract(self.LONG, provider)
        self.assertIn("PASTED CONTENT", provider.last_user)


class TestManualRawJob(unittest.TestCase):
    def test_same_url_yields_the_same_id(self):
        a = manual.make_raw_job(extraction(), "https://x/1")
        b = manual.make_raw_job(extraction(title="Different"), "https://x/1")
        self.assertEqual(a.external_id, b.external_id)

    def test_different_urls_differ(self):
        a = manual.make_raw_job(extraction(), "https://x/1")
        b = manual.make_raw_job(extraction(), "https://x/2")
        self.assertNotEqual(a.external_id, b.external_id)

    def test_missing_company_gets_a_placeholder(self):
        job = manual.make_raw_job(extraction(company=""), "https://x/1")
        self.assertEqual(job.company, "Unknown company")


class TestStoreManual(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.conn = db_module.connect()

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()

    def test_a_manual_source_row_is_created_on_demand(self):
        job = manual.make_raw_job(extraction(), "https://x/1")
        ingest.store_manual(self.conn, job)
        row = self.conn.execute(
            "SELECT * FROM sources WHERE adapter='manual'").fetchone()
        self.assertIsNotNone(row)

    def test_manual_imports_bypass_the_region_filter(self):
        """Pasting a Berlin role by hand is taken as intent, not an accident."""
        job = manual.make_raw_job(extraction(location="Berlin"), "https://x/1")
        job_id = ingest.store_manual(self.conn, job)
        row = self.conn.execute(
            "SELECT stage0_pass, stage0_reason FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["stage0_pass"], 1)
        self.assertIn("hand", row["stage0_reason"])

    def test_reimporting_updates_rather_than_duplicating(self):
        job = manual.make_raw_job(extraction(), "https://x/1")
        first = ingest.store_manual(self.conn, job)
        second = ingest.store_manual(self.conn, job)
        self.assertEqual(first, second)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 1)

    def test_reimporting_a_dismissed_posting_revives_it(self):
        job = manual.make_raw_job(extraction(), "https://x/1")
        job_id = ingest.store_manual(self.conn, job)
        self.conn.execute("UPDATE jobs SET status='dismissed' WHERE id=?", (job_id,))
        self.conn.commit()
        ingest.store_manual(self.conn, job)
        status = self.conn.execute(
            "SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"]
        self.assertEqual(status, "new")


class TestAdapterRegistry(unittest.TestCase):
    def test_every_ats_platform_is_registered(self):
        from jobhunt.sources import ADAPTERS

        for name in ("greenhouse", "lever", "smartrecruiters", "personio",
                     "recruitee", "manual"):
            self.assertIn(name, ADAPTERS, name)

    def test_ashby_is_deliberately_absent(self):
        """Its public API returns no descriptions, so postings cannot be scored."""
        from jobhunt.sources import ADAPTERS

        self.assertNotIn("ashby", ADAPTERS)


if __name__ == "__main__":
    unittest.main()
