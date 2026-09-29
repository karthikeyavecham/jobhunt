"""Sorting and the day filter on the pages that list jobs.

The day filter answers "what came in on Thursday?", so it keys off
`first_seen_at` — when JobHunt found the posting — not `posted_at`, which
several feeds leave blank or approximate.

Berlin is UTC+2 in summer, so the interesting cases are the two hours either
side of local midnight: a posting seen at 00:30 Berlin is stored as 22:30 UTC
the previous day, and must still count as Thursday's.
"""

import os
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jobhunt import app as app_module
from jobhunt import config
from jobhunt import db as db_module
from jobhunt import tracker
from jobhunt.util import day_bounds_utc, get_timezone

BERLIN = get_timezone("Europe/Berlin")


class ListingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)
        os.environ.pop("JOBHUNT_ROLE", None)
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, "
            "last_status) VALUES (1,'S','stub','{}',1,'ok')")
        self.conn.commit()
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()

    def tearDown(self):
        self.conn.close()
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def add_job(self, job_id, company="ACME GmbH", title=None, seen=None,
                score=None, status="new", posted=None):
        title = title or "Role {0}".format(job_id)
        seen = seen or "2026-08-06T09:00:00+00:00"
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, "
            "title, company, location, first_seen_at, last_seen_at, posted_at, "
            "description_text, dedup_key, status, stage0_pass) VALUES "
            "(?,1,?,?,?,?,?,'Köln',?,?,?,'desc',?,?,1)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id,
             "https://x/%s" % job_id, title, company, seen, seen, posted,
             "k%s" % job_id, status))
        if score is not None:
            self.conn.execute(
                "INSERT INTO scores (job_id, fit_score, verdict, matched_json, "
                "gaps_json, rationale, model_used, prompt_version, scored_at) "
                "VALUES (?,?,'good','[]','[]','r','stub','v1',?)",
                (job_id, score, seen))
        self.conn.commit()
        return job_id

    def titles_on(self, query=""):
        """Role titles in the dashboard table, in the order rendered."""
        page = self.client.get("/" + query).get_data(as_text=True)
        return re.findall(r'class="job-title"[^>]*>([^<]+)<', page)


class TestJobSorting(ListingTestCase):
    def setUp(self):
        super(TestJobSorting, self).setUp()
        self.add_job(1, company="Zeta GmbH", title="Older high scorer",
                     seen="2026-08-01T09:00:00+00:00", score=90)
        self.add_job(2, company="Alpha AG", title="Newer low scorer",
                     seen="2026-08-10T09:00:00+00:00", score=40)
        self.add_job(3, company="Mid Co", title="Unscored newest",
                     seen="2026-08-12T09:00:00+00:00")

    def test_default_is_best_fit(self):
        self.assertEqual(self.titles_on()[0], "Older high scorer")

    def test_unscored_sort_last_not_as_zero(self):
        """A scoring backlog must never bury the good matches."""
        self.assertEqual(self.titles_on()[-1], "Unscored newest")

    def test_newest_first(self):
        self.assertEqual(self.titles_on("?sort=newest")[0], "Unscored newest")

    def test_oldest_first(self):
        self.assertEqual(self.titles_on("?sort=oldest")[0], "Older high scorer")

    def test_company_alphabetical(self):
        self.assertEqual(self.titles_on("?sort=company")[0], "Newer low scorer")

    def test_an_unknown_sort_falls_back_to_the_default(self):
        """The value reaches an ORDER BY, so it must be a whitelist, not input."""
        self.assertEqual(self.titles_on("?sort=fit"),
                         self.titles_on("?sort=nonsense"))

    def test_a_sql_injection_attempt_is_ignored(self):
        response = self.client.get("/?sort=fit;DROP+TABLE+jobs--")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 3)

    def test_the_sort_survives_a_status_tab(self):
        page = self.client.get("/?sort=newest").get_data(as_text=True)
        self.assertIn("status=shortlisted&amp;sort=newest", page)


class TestDayFilter(ListingTestCase):
    def test_a_specific_day_shows_only_that_day(self):
        self.add_job(1, title="Thursday job", seen="2026-08-13T09:00:00+00:00")
        self.add_job(2, title="Friday job", seen="2026-08-14T09:00:00+00:00")
        self.assertEqual(self.titles_on("?day=2026-08-13"), ["Thursday job"])

    def test_the_day_is_a_berlin_day_not_a_utc_one(self):
        """00:30 Berlin on the 13th is 22:30 UTC on the 12th."""
        self.add_job(1, title="Just after midnight",
                     seen="2026-08-12T22:30:00+00:00")
        self.assertEqual(self.titles_on("?day=2026-08-13"),
                         ["Just after midnight"])

    def test_just_before_berlin_midnight_belongs_to_the_previous_day(self):
        self.add_job(1, title="Late Tuesday", seen="2026-08-12T21:30:00+00:00")
        self.assertEqual(self.titles_on("?day=2026-08-13"), [])
        self.assertEqual(self.titles_on("?day=2026-08-12"), ["Late Tuesday"])

    def test_the_end_of_the_day_is_exclusive(self):
        """A row must not appear under two days."""
        self.add_job(1, title="Midnight exactly",
                     seen=day_bounds_utc(date(2026, 8, 14), BERLIN)[0])
        self.assertEqual(self.titles_on("?day=2026-08-13"), [])
        self.assertEqual(self.titles_on("?day=2026-08-14"), ["Midnight exactly"])

    def test_last_n_days_includes_today(self):
        today = datetime.now(BERLIN).date()
        self.add_job(1, title="Today", seen=self.at(today, 10))
        self.add_job(2, title="Three days ago",
                     seen=self.at(today - timedelta(days=3), 10))
        self.assertEqual(self.titles_on("?days=1"), ["Today"])
        self.assertIn("Three days ago", self.titles_on("?days=7"))

    def test_an_unreadable_date_is_reported_not_silently_ignored(self):
        self.add_job(1, title="Some job")
        page = self.client.get("/?day=not-a-date").get_data(as_text=True)
        self.assertIn("unreadable date filter", page)
        self.assertIn("Some job", page)

    def test_a_day_with_nothing_says_so_and_offers_a_way_out(self):
        self.add_job(1, seen="2026-08-13T09:00:00+00:00")
        page = self.client.get("/?day=2026-01-01").get_data(as_text=True)
        self.assertIn("Nothing", page)
        self.assertIn("Show every day", page)

    def test_day_wins_over_days_when_both_are_given(self):
        self.add_job(1, title="Old day", seen="2026-08-01T09:00:00+00:00")
        self.assertEqual(self.titles_on("?day=2026-08-01&days=1"), ["Old day"])

    def test_the_filter_respects_the_configured_timezone(self):
        config.set_setting(self.conn, "schedule.timezone", "UTC")
        self.conn.commit()
        self.add_job(1, title="Late UTC evening",
                     seen="2026-08-12T22:30:00+00:00")
        # Berlin would call this the 13th; UTC keeps it on the 12th.
        self.assertEqual(self.titles_on("?day=2026-08-13"), [])
        self.assertEqual(self.titles_on("?day=2026-08-12"), ["Late UTC evening"])

    def test_the_day_picker_lists_days_that_have_postings(self):
        today = datetime.now(BERLIN).date()
        self.add_job(1, seen=self.at(today, 10))
        self.add_job(2, seen=self.at(today, 11))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("{0} — 2 postings".format(today.isoformat()), page)

    def test_the_filter_survives_a_triage_action(self):
        """Shortlisting from a filtered day should come back to that day."""
        self.add_job(1, seen="2026-08-13T09:00:00+00:00")
        page = self.client.get("/?day=2026-08-13").get_data(as_text=True)
        self.assertIn("day=2026-08-13", page)

    @staticmethod
    def at(day, hour):
        local = datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN)
        return local.astimezone(timezone.utc).isoformat()


class TestApplicationSorting(ListingTestCase):
    def setUp(self):
        super(TestApplicationSorting, self).setUp()
        # Dates are relative to today. Fixed dates rotted: a next action set to
        # a "future" 2026-08-20 became overdue ten days later, which pushed the
        # row into the "Due now" panel and broke these tests for a reason that
        # had nothing to do with sorting.
        today = date.today()
        self.soon = (today + timedelta(days=7)).isoformat()
        self.later = (today + timedelta(days=30)).isoformat()

        self.add_job(1, company="Zeta GmbH", title="Offer role", score=50)
        self.add_job(2, company="Alpha AG", title="Applied role", score=90)
        tracker.update(self.conn, 1, pipeline_status="offer",
                       applied_at="2026-06-01", next_action_date=self.later)
        tracker.update(self.conn, 2, pipeline_status="applied",
                       applied_at="2026-07-15", next_action_date=self.soon)

    def roles(self, query=""):
        """Role titles from the "All applications" table only.

        Scoped to that section: the "Due now" and "stale" panels above it also
        use `class="job-title"`, and render the *company* rather than the role.
        """
        page = self.client.get("/applications" + query).get_data(as_text=True)
        table = page.split('id="all-applications"', 1)[-1]
        return re.findall(r'class="job-title"[^>]*>([^<]+)<', table)

    def test_default_is_pipeline_stage(self):
        """Applied comes before offer in the pipeline, so it leads."""
        self.assertEqual(self.roles()[0], "Applied role")

    def test_sort_by_most_recently_applied(self):
        self.assertEqual(self.roles("?sort=applied")[0], "Applied role")

    def test_sort_by_next_action_due(self):
        self.assertEqual(self.roles("?sort=due")[0], "Applied role")

    def test_sort_by_fit(self):
        self.assertEqual(self.roles("?sort=fit")[0], "Applied role")

    def test_sort_by_company(self):
        self.assertEqual(self.roles("?sort=company")[0], "Applied role")

    def test_an_unknown_sort_falls_back_to_stage(self):
        self.assertEqual(self.roles("?sort=nonsense"), self.roles())

    def test_rows_without_a_sort_key_still_appear(self):
        """A NULL applied date must not drop the row from the list."""
        self.add_job(3, company="Beta", title="Never applied", score=10)
        tracker.update(self.conn, 3, pipeline_status="shortlisted")
        self.assertIn("Never applied", self.roles("?sort=applied"))


if __name__ == "__main__":
    unittest.main()
