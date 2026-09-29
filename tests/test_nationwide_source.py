"""The nationwide Bundesagentur source, and the scoring-backlog figure.

Added 2026-08-13 after checking whether make-it-in-germany.com was worth
adding as a source. It is a translated front-end over the Bundesagentur
Jobbörse — the same postings, under the same reference numbers, that JobHunt
already fetches from the official API — so the gap was never the data. It was
the query: the existing source is pinned to 50km around Köln, which cannot
return a remote or relocation role advertised in Berlin.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from jobhunt import app as app_module
from jobhunt import config, scoring
from jobhunt import db as db_module
from jobhunt.util import now_iso


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def sources(self, conn):
        return {row["name"]: row for row in
                conn.execute("SELECT * FROM sources").fetchall()}


class TestNationwideSourceIsInstalled(DatabaseTestCase):
    NAME = "Bundesagentur für Arbeit (nationwide)"

    def test_a_fresh_database_gets_it(self):
        db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        self.assertIn(self.NAME, self.sources(conn))

    def test_an_existing_database_gets_it_too(self):
        """The Pi's database predates it, and `_seed` only fills empty tables."""
        db_module.init_db()
        conn = db_module.connect()
        conn.execute("DELETE FROM sources WHERE name = ?", (self.NAME,))
        conn.commit()
        conn.close()

        db_module.init_db()                      # a later start
        conn = db_module.connect()
        self.addCleanup(conn.close)
        self.assertIn(self.NAME, self.sources(conn))

    def test_it_is_not_added_twice(self):
        for _ in range(3):
            db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT COUNT(*) FROM sources WHERE name = ?",
                            (self.NAME,)).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_deleting_it_deliberately_makes_it_stay_gone(self):
        """Re-adding a source the user removed would be its own kind of bug."""
        db_module.init_db()
        conn = db_module.connect()
        conn.execute("DELETE FROM sources WHERE name = ?", (self.NAME,))
        config.set_setting(conn, "sources.removed", [self.NAME])
        conn.commit()
        conn.close()

        db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        self.assertNotIn(self.NAME, self.sources(conn))

    def test_its_config_searches_nationwide_for_salaried_roles(self):
        db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        cfg = json.loads(self.sources(conn)[self.NAME]["config_json"])
        self.assertEqual(cfg["wo"], "")           # no geographic filter
        self.assertEqual(cfg["angebotsart"], 1)   # salaried, not apprenticeships
        self.assertEqual(cfg["veroeffentlichtseit"], 7)

    def test_the_window_is_shorter_than_the_regional_source(self):
        """Nationwide matches ~11x as much; a 30-day first run would queue
        over a thousand scorings at 40 per cycle."""
        db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        found = self.sources(conn)
        national = json.loads(found[self.NAME]["config_json"])
        regional = json.loads(found["Bundesagentur für Arbeit"]["config_json"])
        self.assertLess(national["veroeffentlichtseit"],
                        regional["veroeffentlichtseit"])

    def test_the_regional_source_is_left_alone(self):
        db_module.init_db()
        conn = db_module.connect()
        self.addCleanup(conn.close)
        cfg = json.loads(self.sources(conn)["Bundesagentur für Arbeit"]["config_json"])
        self.assertEqual(cfg["wo"], "Köln")
        self.assertEqual(cfg["umkreis"], 50)


class TestDeletingASourceRemembers(DatabaseTestCase):
    def test_removal_is_recorded(self):
        db_module.init_db()
        conn = db_module.connect()
        source_id = conn.execute(
            "SELECT id FROM sources WHERE name = ?",
            ("Bundesagentur für Arbeit (nationwide)",)).fetchone()[0]
        conn.close()

        application = app_module.create_app()
        application.config["TESTING"] = True
        application.test_client().post(
            "/sources/{0}/delete".format(source_id), follow_redirects=True)

        conn = db_module.connect()
        self.addCleanup(conn.close)
        removed = config.get_setting(conn, "sources.removed", [])
        self.assertIn("Bundesagentur für Arbeit (nationwide)", removed)


class TestScoringBacklog(DatabaseTestCase):
    """The dashboard's "Awaiting scoring" figure."""

    def setUp(self):
        super(TestScoringBacklog, self).setUp()
        db_module.init_db()
        self.conn = db_module.connect()
        self.addCleanup(self.conn.close)

    def add_job(self, job_id, status="new", stage0=1, scored=False):
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, "
            "title, company, first_seen_at, last_seen_at, description_text, "
            "dedup_key, status, stage0_pass) VALUES "
            "(?,1,?,?,?,'T','C',?,?,'d',?,?,?)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id,
             "https://x/%s" % job_id, now_iso(), now_iso(), "k%s" % job_id,
             status, stage0))
        if scored:
            self.conn.execute(
                "INSERT INTO scores (job_id, fit_score, verdict, matched_json, "
                "gaps_json, rationale, model_used, prompt_version, scored_at) "
                "VALUES (?,70,'good','[]','[]','r','stub','v1',?)",
                (job_id, now_iso()))
        self.conn.commit()

    def test_counts_unscored_relevant_postings(self):
        self.add_job(1)
        self.add_job(2)
        self.assertEqual(scoring.pending_count(self.conn), 2)

    def test_scored_postings_do_not_count(self):
        self.add_job(1, scored=True)
        self.assertEqual(scoring.pending_count(self.conn), 0)

    def test_filtered_out_postings_do_not_count(self):
        """Out-of-region postings are never scored, so they are not a backlog."""
        self.add_job(1, stage0=0)
        self.assertEqual(scoring.pending_count(self.conn), 0)

    def test_dismissed_postings_do_not_count(self):
        self.add_job(1, status="dismissed")
        self.assertEqual(scoring.pending_count(self.conn), 0)

    def test_it_agrees_with_what_the_scorer_will_actually_do(self):
        """The number on screen has to be the number that gets worked through."""
        for i in range(1, 6):
            self.add_job(i)
        self.add_job(6, scored=True)
        self.add_job(7, stage0=0)
        self.assertEqual(scoring.pending_count(self.conn),
                         len(scoring.pending_jobs(self.conn)))

    def test_the_dashboard_shows_it(self):
        for i in range(1, 4):
            self.add_job(i)
        application = app_module.create_app()
        application.config["TESTING"] = True
        page = application.test_client().get("/").get_data(as_text=True)
        self.assertIn("Awaiting scoring", page)
        block = page.split("Awaiting scoring", 1)[1][:200]
        self.assertIn("3", block)

    def test_an_empty_backlog_says_so_rather_than_showing_a_bare_zero(self):
        application = app_module.create_app()
        application.config["TESTING"] = True
        page = application.test_client().get("/").get_data(as_text=True)
        self.assertIn("all caught up", page)

    def test_the_hint_scales_the_backlog_in_time(self):
        hint = app_module._backlog_hint(1200, batch=40, interval_hours=3)
        self.assertIn("day", hint)
        self.assertIn("40/cycle", hint)

    def test_a_small_backlog_reads_as_one_cycle(self):
        self.assertIn("next cycle",
                      app_module._backlog_hint(10, batch=40, interval_hours=3))

    def test_a_medium_backlog_reads_in_hours(self):
        self.assertIn("h at", app_module._backlog_hint(100, batch=40,
                                                       interval_hours=3))


if __name__ == "__main__":
    unittest.main()
