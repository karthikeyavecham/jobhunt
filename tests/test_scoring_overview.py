"""The scoring overview: progress per day, and scoring one day on demand.

Built when the relocation filter turned ~3,800 postings relevant overnight and
the queue became about twelve days long at the default batch size. Working
through it strictly newest-first is not always what is wanted — sometimes one
particular day's intake matters more than clearing the queue in order.

Days are Berlin days throughout, so the interesting cases sit either side of
local midnight: 00:30 Berlin is stored as 22:30 UTC the previous day.
"""

import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jobhunt import app as app_module
from jobhunt import llm, scoring, tasks
from jobhunt import db as db_module
from jobhunt.util import day_bounds_utc, get_timezone
from jobhunt.tests.test_scoring import full_result

BERLIN = get_timezone("Europe/Berlin")


class ScoringOverviewTestCase(unittest.TestCase):
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
        self.addCleanup(self.conn.close)
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, "
            "last_status) VALUES (1,'S','stub','{}',1,'ok')")
        self.conn.commit()
        tasks.registry.clear_history()
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()

    def tearDown(self):
        tasks.registry.clear_history()
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def add_job(self, job_id, seen, scored=False, stage0=1, status="new"):
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, "
            "title, company, location, first_seen_at, last_seen_at, "
            "description_text, dedup_key, status, stage0_pass) VALUES "
            "(?,1,?,?,?,?,'ACME','Köln',?,?,'desc',?,?,?)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id,
             "https://x/%s" % job_id, "Role %s" % job_id, seen, seen,
             "k%s" % job_id, status, stage0))
        if scored:
            self.conn.execute(
                "INSERT INTO scores (job_id, fit_score, verdict, matched_json, "
                "gaps_json, rationale, model_used, prompt_version, scored_at) "
                "VALUES (?,70,'good','[]','[]','r','stub','v1',?)",
                (job_id, seen))
        self.conn.commit()
        return job_id

    def progress(self, days=None):
        return scoring.daily_progress(self.conn, BERLIN, days=days)

    def by_day(self, days=None):
        return {row["day"]: row for row in self.progress(days)}


class TestDailyProgress(ScoringOverviewTestCase):
    def test_counts_scored_and_pending_per_day(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        self.add_job(2, "2026-09-10T10:00:00+00:00")
        self.add_job(3, "2026-09-11T09:00:00+00:00")
        rows = self.by_day()
        self.assertEqual(rows["2026-09-10"]["total"], 2)
        self.assertEqual(rows["2026-09-10"]["scored"], 1)
        self.assertEqual(rows["2026-09-10"]["pending"], 1)
        self.assertEqual(rows["2026-09-11"]["pending"], 1)

    def test_newest_day_first(self):
        self.add_job(1, "2026-09-01T09:00:00+00:00")
        self.add_job(2, "2026-09-11T09:00:00+00:00")
        self.assertEqual([r["day"] for r in self.progress()],
                         ["2026-09-11", "2026-09-01"])

    def test_days_are_berlin_days(self):
        """00:30 Berlin on the 11th is 22:30 UTC on the 10th."""
        self.add_job(1, "2026-09-10T22:30:00+00:00")
        self.assertIn("2026-09-11", self.by_day())

    def test_the_day_boundary_is_exclusive_at_the_end(self):
        """A posting must not be counted under two days."""
        start = day_bounds_utc(date(2026, 9, 11), BERLIN)[0]
        self.add_job(1, start)
        rows = self.by_day()
        self.assertEqual(rows["2026-09-11"]["total"], 1)
        self.assertNotIn("2026-09-10", rows)

    def test_out_of_region_postings_are_excluded(self):
        """They are never scored, so they are not scoring progress."""
        self.add_job(1, "2026-09-10T09:00:00+00:00", stage0=0)
        self.assertEqual(self.progress(), [])

    def test_dismissed_and_unscored_counts_as_skipped_not_pending(self):
        """It will never be scored, so counting it as pending would promise
        work that never happens."""
        self.add_job(1, "2026-09-10T09:00:00+00:00", status="dismissed")
        row = self.by_day()["2026-09-10"]
        self.assertEqual(row["pending"], 0)
        self.assertEqual(row["skipped"], 1)
        self.assertEqual(row["total"], 1)

    def test_dismissed_but_scored_still_counts_as_scored(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True,
                     status="dismissed")
        row = self.by_day()["2026-09-10"]
        self.assertEqual(row["scored"], 1)
        self.assertEqual(row["skipped"], 0)

    def test_the_columns_always_add_up(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        self.add_job(2, "2026-09-10T09:00:00+00:00")
        self.add_job(3, "2026-09-10T09:00:00+00:00", status="dismissed")
        row = self.by_day()["2026-09-10"]
        self.assertEqual(row["total"],
                         row["scored"] + row["pending"] + row["skipped"])

    def test_the_day_window_bounds_the_scan(self):
        today = datetime.now(BERLIN).date()
        self.add_job(1, self.at(today))
        self.add_job(2, self.at(today - timedelta(days=40)))
        self.assertEqual(len(self.progress(days=7)), 1)
        self.assertEqual(len(self.progress()), 2)

    def test_the_pending_total_agrees_with_pending_count(self):
        """The page's figure and the scorer's queue must be the same number."""
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.add_job(2, "2026-09-11T09:00:00+00:00", scored=True)
        self.add_job(3, "2026-09-11T09:00:00+00:00", status="dismissed")
        self.assertEqual(sum(r["pending"] for r in self.progress()),
                         scoring.pending_count(self.conn))

    @staticmethod
    def at(day, hour=9):
        local = datetime(day.year, day.month, day.day, hour, tzinfo=BERLIN)
        return local.astimezone(timezone.utc).isoformat()


class TestPendingWindow(ScoringOverviewTestCase):
    """`since`/`until` on the pending queries — what scoring one day uses."""

    def setUp(self):
        super(TestPendingWindow, self).setUp()
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.add_job(2, "2026-09-11T09:00:00+00:00")
        self.add_job(3, "2026-09-11T14:00:00+00:00")
        self.since, self.until = day_bounds_utc(date(2026, 9, 11), BERLIN)

    def test_counts_only_that_day(self):
        self.assertEqual(
            scoring.pending_count(self.conn, since=self.since, until=self.until), 2)

    def test_selects_only_that_day(self):
        jobs = scoring.pending_jobs(self.conn, since=self.since, until=self.until)
        self.assertEqual(sorted(j["id"] for j in jobs), [2, 3])

    def test_no_window_means_everything(self):
        self.assertEqual(scoring.pending_count(self.conn), 3)

    def test_a_window_with_nothing_in_it_is_empty_not_an_error(self):
        since, until = day_bounds_utc(date(2020, 1, 1), BERLIN)
        self.assertEqual(scoring.pending_count(self.conn, since, until), 0)
        self.assertEqual(scoring.pending_jobs(self.conn, since=since,
                                              until=until), [])


class StubProvider(llm.Provider):
    """Scores instantly and records what it was asked about.

    Subclasses the real Provider rather than duck-typing it — `score_job` calls
    `model_label()` — and borrows `test_scoring.full_result` for the payload
    shape rather than keeping a second copy of it that can drift.
    """

    name, model = "stub", "stub-1"

    def __init__(self):
        self.seen = []

    def complete_json(self, system, user, json_schema, max_tokens=1500,
                      temperature=0.1):
        self.seen.append(user)
        return full_result()


class TestScoringOneDay(ScoringOverviewTestCase):
    def test_score_pending_honours_the_window(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.add_job(2, "2026-09-11T09:00:00+00:00")
        since, until = day_bounds_utc(date(2026, 9, 11), BERLIN)
        run = scoring.score_pending(self.conn, StubProvider(), since=since,
                                    until=until)
        self.assertEqual(run.scored, 1)
        self.assertEqual(scoring.pending_count(self.conn), 1)
        remaining = scoring.pending_jobs(self.conn)
        self.assertEqual(remaining[0]["id"], 1)


class TestScoringPage(ScoringOverviewTestCase):
    def page(self):
        return self.client.get("/scoring").get_data(as_text=True)

    def test_the_page_lists_a_row_per_day(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        self.add_job(2, "2026-09-11T09:00:00+00:00")
        page = self.page()
        self.assertIn("2026-09-10", page)
        self.assertIn("2026-09-11", page)

    def test_the_headline_totals_are_shown(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        self.add_job(2, "2026-09-10T09:00:00+00:00")
        page = self.page()
        self.assertIn("Awaiting scoring", page)
        self.assertIn("50% complete", page)

    def test_a_fully_scored_day_offers_no_button(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        page = self.page()
        self.assertNotIn("Score 1", page)
        self.assertIn("done", page)

    def test_a_day_with_work_offers_a_button_naming_the_count(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.add_job(2, "2026-09-10T10:00:00+00:00")
        self.assertIn("Score 2", self.page())

    def test_the_day_links_to_that_day_on_the_dashboard(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.assertIn("day=2026-09-10", self.page())

    def test_an_empty_database_says_so(self):
        self.assertIn("Nothing fetched yet", self.page())

    def test_the_nav_links_to_it(self):
        self.assertIn('href="/scoring"', self.client.get("/").get_data(as_text=True))

    def test_dismissed_before_scoring_is_explained_not_hidden(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", status="dismissed")
        self.assertIn("dismissed before scoring", self.page())


class TestScoreDayRoute(ScoringOverviewTestCase):
    """The registry is process-wide, so a run left going here leaks into the
    next test. The provider is stubbed so background work finishes instantly
    rather than retrying a dead endpoint with backoff."""

    def setUp(self):
        super(TestScoreDayRoute, self).setUp()
        patcher = mock.patch("jobhunt.config.build_provider",
                             return_value=StubProvider())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.wait)

    def post(self, **data):
        return self.client.post("/scoring/run", data=data,
                                follow_redirects=True).get_data(as_text=True)

    def wait(self, key="score", timeout=10.0):
        import time
        deadline = time.time() + timeout
        while tasks.registry.is_running(key) and time.time() < deadline:
            time.sleep(0.01)

    def test_an_unparseable_day_is_refused(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.assertIn("Not a date", self.post(day="last tuesday"))

    def test_a_day_with_nothing_outstanding_says_so(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00", scored=True)
        self.assertIn("Nothing outstanding", self.post(day="2026-09-10"))

    def test_a_day_with_work_starts_a_background_task(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        page = self.post(day="2026-09-10")
        self.assertIn("in the background", page)
        self.assertIn("1 posting", page)
        self.wait()

    def test_the_task_is_labelled_with_the_day(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.post(day="2026-09-10")
        self.wait()
        labels = [t.label for t in tasks.registry.recent()]
        self.assertTrue(any("2026-09-10" in label for label in labels), labels)

    def test_a_second_run_is_refused_while_one_is_going(self):
        """Two runs would have the local model fighting itself."""
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        gate = threading.Event()
        tasks.registry.start("score", "Score outstanding postings", gate.wait)
        try:
            self.assertIn("already running", self.post(day="2026-09-10"))
        finally:
            gate.set()
            self.wait()

    def test_it_shares_the_key_with_the_dashboard_button(self):
        """Both hit the same local model; the registry key is the guard."""
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        gate = threading.Event()
        tasks.registry.start("score", "Score outstanding postings", gate.wait)
        try:
            self.assertIn("already running", self.post())
        finally:
            gate.set()
            self.wait()

    def test_scoring_everything_needs_no_day(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.assertIn("in the background", self.post())
        self.wait()

    def test_a_day_outside_the_data_is_reported_not_started(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.assertIn("Nothing outstanding", self.post(day="1999-01-01"))


class CountingProvider(StubProvider):
    """Counts how many calls are in flight at once."""

    def __init__(self, delay=0.05, fail_on=None, unavailable_on=None):
        super(CountingProvider, self).__init__()
        self.delay = delay
        self.fail_on = fail_on
        self.unavailable_on = unavailable_on
        self.calls = 0
        self.peak_concurrent = 0
        self._live = 0
        self._lock = threading.Lock()

    def complete_json(self, system, user, json_schema, max_tokens=1500,
                      temperature=0.1):
        import time
        with self._lock:
            self.calls += 1
            call = self.calls
            self._live += 1
            self.peak_concurrent = max(self.peak_concurrent, self._live)
        try:
            time.sleep(self.delay)
            if self.unavailable_on and call >= self.unavailable_on:
                raise llm.ProviderUnavailable("mac asleep")
            if self.fail_on and call == self.fail_on:
                raise llm.ProviderFailed("bad json")
            return full_result()
        finally:
            with self._lock:
                self._live -= 1


class TestParallelScoring(ScoringOverviewTestCase):
    """`scoring.concurrency` > 1.

    Worth having because LM Studio batches concurrent requests rather than
    serialising them — measured 2.2 jobs/min at one worker against 5.1 at four.
    """

    def add_jobs(self, count, seen="2026-09-10T09:00:00+00:00"):
        for i in range(1, count + 1):
            self.add_job(i, seen)

    def test_one_worker_is_still_sequential(self):
        self.add_jobs(4)
        provider = CountingProvider()
        run = scoring.score_pending(self.conn, provider, concurrency=1)
        self.assertEqual(run.scored, 4)
        self.assertEqual(provider.peak_concurrent, 1)

    def test_workers_actually_run_at_the_same_time(self):
        self.add_jobs(8)
        provider = CountingProvider(delay=0.15)
        scoring.score_pending(self.conn, provider, concurrency=4)
        self.assertGreater(provider.peak_concurrent, 1)
        self.assertLessEqual(provider.peak_concurrent, 4)

    def test_every_job_is_scored_exactly_once(self):
        """The shared queue must not hand the same posting to two workers."""
        self.add_jobs(12)
        provider = CountingProvider()
        run = scoring.score_pending(self.conn, provider, concurrency=4)
        self.assertEqual(run.scored, 12)
        self.assertEqual(provider.calls, 12)
        self.assertEqual(scoring.pending_count(self.conn), 0)

    def test_results_are_written_from_every_worker(self):
        self.add_jobs(8)
        scoring.score_pending(self.conn, CountingProvider(), concurrency=4)
        stored = self.conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        self.assertEqual(stored, 8)

    def test_a_failing_job_does_not_stop_the_others(self):
        self.add_jobs(6)
        run = scoring.score_pending(self.conn, CountingProvider(fail_on=2),
                                    concurrency=3)
        self.assertEqual(run.failed, 1)
        self.assertEqual(run.scored, 5)
        self.assertEqual(len(run.errors), 1)

    def test_an_unavailable_provider_stops_the_whole_run(self):
        """Unreachable for one worker means unreachable for all — five retries
        each across every remaining posting would take hours."""
        self.add_jobs(20)
        provider = CountingProvider(unavailable_on=3)
        run = scoring.score_pending(self.conn, provider, concurrency=4)
        self.assertIsNotNone(run.aborted_reason)
        self.assertIn("mac asleep", run.aborted_reason)
        self.assertLess(provider.calls, 20)

    def test_the_window_still_applies_in_parallel(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        self.add_job(2, "2026-09-11T09:00:00+00:00")
        self.add_job(3, "2026-09-11T10:00:00+00:00")
        since, until = day_bounds_utc(date(2026, 9, 11), BERLIN)
        run = scoring.score_pending(self.conn, CountingProvider(),
                                    concurrency=3, since=since, until=until)
        self.assertEqual(run.scored, 2)
        self.assertEqual([j["id"] for j in scoring.pending_jobs(self.conn)], [1])

    def test_the_limit_still_applies_in_parallel(self):
        self.add_jobs(10)
        run = scoring.score_pending(self.conn, CountingProvider(),
                                    concurrency=4, limit=4)
        self.assertEqual(run.scored, 4)
        self.assertEqual(scoring.pending_count(self.conn), 6)

    def test_more_workers_than_jobs_is_harmless(self):
        self.add_jobs(2)
        run = scoring.score_pending(self.conn, CountingProvider(), concurrency=8)
        self.assertEqual(run.scored, 2)

    def test_zero_or_none_falls_back_to_one_worker(self):
        """A blank or zeroed config field must not mean 'no workers' and hang."""
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        for value in (0, None, "", 1):
            self.conn.execute("DELETE FROM scores")
            self.conn.commit()
            run = scoring.score_pending(self.conn, CountingProvider(),
                                        concurrency=value)
            self.assertEqual(run.scored, 1, repr(value))

    def test_nothing_pending_starts_no_threads(self):
        provider = CountingProvider()
        run = scoring.score_pending(self.conn, provider, concurrency=4)
        self.assertEqual(run.scored, 0)
        self.assertEqual(provider.calls, 0)


class TestConcurrencySetting(ScoringOverviewTestCase):
    def test_it_defaults_to_one(self):
        from jobhunt import config
        self.assertEqual(config.get_setting(self.conn, "scoring.concurrency"), 1)

    def test_the_config_form_can_write_it(self):
        self.client.post("/config/save",
                         data={"section": "scoring", "scoring.enabled": "1",
                               "scoring.batch_limit": "40",
                               "scoring.concurrency": "4"},
                         follow_redirects=True)
        from jobhunt import config
        self.assertEqual(config.get_setting(self.conn, "scoring.concurrency"), 4)

    def test_the_config_page_offers_it(self):
        page = self.client.get("/config").get_data(as_text=True)
        self.assertIn('name="scoring.concurrency"', page)
        self.assertIn('name="scoring.batch_limit"', page)

    def test_the_scoring_page_says_how_many_run_at_once(self):
        self.add_job(1, "2026-09-10T09:00:00+00:00")
        page = self.client.get("/scoring").get_data(as_text=True)
        self.assertIn("1 at a time", page)


if __name__ == "__main__":
    unittest.main()
