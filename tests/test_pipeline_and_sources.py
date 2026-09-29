"""Pipeline transitions, source CRUD, background tasks and tracker I/O.

These cover the four review findings fixed in this batch: A1 (the tracker
dead-ended at "applied"), A2 (five ATS adapters with no way to add one),
A3 (routes blocking the browser for minutes), and C (no export or import).
"""

import io
import os
import re
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path

from jobhunt import app as app_module
from jobhunt import db as db_module
from jobhunt import tasks, tracker, tracker_io


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, last_status)"
            " VALUES (1,'S','stub','{}',1,'ok')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()

    def add_job(self, job_id, company="ACME GmbH", title="Support Ops Manager"):
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, title,"
            " company, location, first_seen_at, last_seen_at, description_text,"
            " dedup_key, status, stage0_pass) VALUES "
            "(?,1,?,?,?,?,?,'Köln','2026-08-06T00:00:00+00:00',"
            "'2026-08-06T00:00:00+00:00','desc',?,'new',1)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id, "https://x/%s" % job_id,
             title, company, "k%s" % job_id))
        self.conn.commit()
        return job_id


class TestPipelineTransitions(TrackerTestCase):
    """A1: the tracker used to dead-end at 'applied'."""

    def test_every_stage_is_reachable(self):
        self.add_job(1)
        for stage in tracker.PIPELINE:
            row = tracker.update(self.conn, 1, pipeline_status=stage)
            self.assertEqual(row["pipeline_status"], stage)

    def test_reaching_interview_backfills_the_applied_date(self):
        """A role that jumps straight to interview still went out somehow."""
        self.add_job(1)
        row = tracker.update(self.conn, 1, pipeline_status="interview")
        self.assertEqual(row["applied_at"], date.today().isoformat())

    def test_an_explicit_applied_date_is_not_overwritten(self):
        self.add_job(1)
        tracker.update(self.conn, 1, pipeline_status="applied",
                       applied_at="2026-06-15")
        row = tracker.update(self.conn, 1, pipeline_status="offer")
        self.assertEqual(row["applied_at"], "2026-06-15")

    def test_shortlisted_does_not_backfill_an_applied_date(self):
        self.add_job(1)
        row = tracker.update(self.conn, 1, pipeline_status="shortlisted")
        self.assertIsNone(row["applied_at"])

    def test_backfill_can_be_switched_off_for_historical_rows(self):
        """The importer must not date a June application to today."""
        self.add_job(1)
        row = tracker.update(self.conn, 1, pipeline_status="applied",
                             backfill_applied=False)
        self.assertIsNone(row["applied_at"])

    def test_blank_fields_leave_existing_values_alone(self):
        """The form submits every field; empty must mean 'unchanged'."""
        self.add_job(1)
        tracker.update(self.conn, 1, pipeline_status="applied",
                       notes="Spoke to the recruiter", next_action="Follow up")
        tracker.update(self.conn, 1, pipeline_status="applied")
        row = tracker.get(self.conn, 1)
        self.assertEqual(row["notes"], "Spoke to the recruiter")
        self.assertEqual(row["next_action"], "Follow up")

    def test_closing_a_role_takes_it_out_of_triage(self):
        self.add_job(1)
        tracker.update(self.conn, 1, pipeline_status="rejected")
        status = self.conn.execute(
            "SELECT status FROM jobs WHERE id=1").fetchone()["status"]
        self.assertEqual(status, "dismissed")

    def test_reopening_puts_it_back(self):
        self.add_job(1)
        tracker.update(self.conn, 1, pipeline_status="withdrawn")
        tracker.update(self.conn, 1, pipeline_status="interview")
        status = self.conn.execute(
            "SELECT status FROM jobs WHERE id=1").fetchone()["status"]
        self.assertEqual(status, "shortlisted")

    def test_unknown_status_is_rejected(self):
        self.add_job(1)
        with self.assertRaises(ValueError):
            tracker.update(self.conn, 1, pipeline_status="hired-probably")

    def test_unknown_field_is_ignored_not_written(self):
        self.add_job(1)
        tracker.update(self.conn, 1, pipeline_status="applied",
                       pipeline_status_evil="x", salary="1000000")
        self.assertEqual(tracker.get(self.conn, 1)["pipeline_status"], "applied")

    def test_update_creates_the_row_when_missing(self):
        self.add_job(1)
        self.assertIsNone(tracker.get(self.conn, 1))
        tracker.update(self.conn, 1, pipeline_status="interview")
        self.assertIsNotNone(tracker.get(self.conn, 1))

    def test_all_applications_orders_by_pipeline_stage(self):
        for index, stage in enumerate(("offer", "shortlisted", "interview"), start=1):
            self.add_job(index, company="C%d" % index)
            tracker.update(self.conn, index, pipeline_status=stage)
        order = [r["pipeline_status"] for r in tracker.all_applications(self.conn)]
        self.assertEqual(order, ["shortlisted", "interview", "offer"])


class TestExport(TrackerTestCase):
    def setUp(self):
        TrackerTestCase.setUp(self)
        self.add_job(1, company="FREE NOW", title="Zendesk Platform Owner")
        tracker.update(self.conn, 1, pipeline_status="interview",
                       notes="First round", next_action="Prep STAR stories")

    def test_json_export_round_trips(self):
        import json

        payload = json.loads(tracker_io.to_json(self.conn).decode("utf-8"))
        self.assertEqual(len(payload["applications"]), 1)
        self.assertEqual(payload["applications"][0]["company"], "FREE NOW")

    def test_xlsx_export_is_a_real_workbook(self):
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(tracker_io.to_xlsx(self.conn)))
        sheet = book.active
        self.assertEqual(sheet.title, "Applications")
        self.assertEqual(sheet.cell(row=1, column=1).value, "Company")
        self.assertEqual(sheet.cell(row=2, column=1).value, "FREE NOW")
        self.assertEqual(sheet.freeze_panes, "A2")


class TestImportMapping(unittest.TestCase):
    """Real trackers use whatever headers their author felt like."""

    def test_common_header_spellings_map(self):
        mapping, unknown = tracker_io.map_headers(
            ["Employer", "Job Title", "Application Status", "Date Applied",
             "Referred By", "Comments"])
        self.assertEqual(mapping["company"], 0)
        self.assertEqual(mapping["title"], 1)
        self.assertEqual(mapping["pipeline_status"], 2)
        self.assertEqual(mapping["applied_at"], 3)
        self.assertEqual(mapping["referral"], 4)
        self.assertEqual(mapping["notes"], 5)
        self.assertEqual(unknown, [])

    def test_unrecognised_headers_are_reported_not_dropped_silently(self):
        _, unknown = tracker_io.map_headers(["Company", "Salary expectation"])
        self.assertEqual(unknown, ["Salary expectation"])

    def test_status_synonyms_normalise(self):
        for raw, expected in [("Applied", "applied"), ("Rejected", "rejected"),
                              ("Interviewing", "interview"), ("To send", "shortlisted"),
                              ("Phone screen", "screening"), ("Offer", "offer")]:
            self.assertEqual(tracker_io.normalise_status(raw), expected, raw)

    def test_unknown_status_defaults_to_applied(self):
        """A row in someone's tracker means they applied."""
        self.assertEqual(tracker_io.normalise_status("¯\\_(ツ)_/¯"), "applied")

    def test_german_and_iso_dates_both_parse(self):
        self.assertEqual(tracker_io.normalise_date("15.06.2026"), "2026-06-15")
        self.assertEqual(tracker_io.normalise_date("2026-06-15"), "2026-06-15")
        self.assertEqual(tracker_io.normalise_date("15/06/2026"), "2026-06-15")

    def test_unparseable_date_is_none_not_a_guess(self):
        self.assertIsNone(tracker_io.normalise_date("next Tuesday"))
        self.assertIsNone(tracker_io.normalise_date(""))


class TestImportEndToEnd(TrackerTestCase):
    CSV = (b"Employer;Job Title;Application Status;Date Applied;Referred By;Comments\n"
           b"FREE NOW;Zendesk Platform Owner;Interview;20.06.2026;;Top priority\n"
           b"JUPUS GmbH;Technical Support Manager;Screening;22.06.2026;Peter H;Referral\n")

    def test_rows_become_jobs_and_applications(self):
        result = tracker_io.import_applications(self.conn, self.CSV, "tracker.csv")
        self.assertEqual(result.imported, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 2)

    def test_fields_survive_the_round_trip(self):
        tracker_io.import_applications(self.conn, self.CSV, "tracker.csv")
        row = self.conn.execute(
            "SELECT a.*, j.company FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.company='JUPUS GmbH'").fetchone()
        self.assertEqual(row["pipeline_status"], "screening")
        self.assertEqual(row["applied_at"], "2026-06-22")
        self.assertEqual(row["referral"], "Peter H")

    def test_a_blank_applied_column_stays_blank(self):
        """Importing history must not stamp every row with today's date.

        The tracker template ships with the dates blank because they were not
        known; inventing them here would also reset the follow-up nudges to
        zero days old for applications that went out weeks ago.
        """
        csv = (b"Company,Role,Status,Applied\n"
               b"Rossmann,Product Owner,,\n")
        tracker_io.import_applications(self.conn, csv, "tracker.csv")
        row = self.conn.execute(
            "SELECT a.* FROM applications a JOIN jobs j ON j.id=a.job_id "
            "WHERE j.company='Rossmann'").fetchone()
        self.assertEqual(row["pipeline_status"], "applied")
        self.assertIsNone(row["applied_at"])

    def test_reimport_updates_rather_than_duplicating(self):
        tracker_io.import_applications(self.conn, self.CSV, "tracker.csv")
        second = tracker_io.import_applications(self.conn, self.CSV, "tracker.csv")
        self.assertEqual(second.imported, 0)
        self.assertEqual(second.updated, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 2)

    def test_a_file_with_no_usable_columns_fails_loudly(self):
        with self.assertRaises(ValueError):
            tracker_io.import_applications(
                self.conn, b"Colour;Size\nred;large\n", "junk.csv")

    def test_blank_rows_are_skipped_not_imported(self):
        data = self.CSV + b";;;;;\n"
        result = tracker_io.import_applications(self.conn, data, "t.csv")
        self.assertEqual(result.imported, 2)

    def test_imported_rows_bypass_the_region_filter(self):
        tracker_io.import_applications(self.conn, self.CSV, "t.csv")
        rows = self.conn.execute(
            "SELECT stage0_pass FROM jobs WHERE external_id LIKE 'import-%'").fetchall()
        self.assertTrue(all(r["stage0_pass"] == 1 for r in rows))


class TestBackgroundTasks(unittest.TestCase):
    """A3: the registry that keeps slow work off the request thread."""

    def setUp(self):
        self.registry = tasks.Registry()

    def wait(self, key, timeout=3.0):
        import time

        deadline = time.time() + timeout
        while self.registry.is_running(key) and time.time() < deadline:
            time.sleep(0.01)

    def test_a_task_runs_and_records_its_summary(self):
        self.registry.start("k", "Label", lambda: "42 done")
        self.wait("k")
        recent = self.registry.recent()
        self.assertEqual(recent[0].result, "42 done")
        self.assertIsNone(recent[0].error)

    def test_a_second_start_for_a_live_key_is_refused(self):
        import threading

        gate = threading.Event()
        self.registry.start("k", "Label", gate.wait)
        try:
            self.assertFalse(self.registry.start("k", "Label", lambda: "second"))
        finally:
            gate.set()
        self.wait("k")

    def test_a_failing_task_records_the_error_and_frees_the_key(self):
        def boom():
            raise RuntimeError("kaboom")

        self.registry.start("k", "Label", boom)
        self.wait("k")
        self.assertIn("kaboom", self.registry.recent()[0].error)
        self.assertTrue(self.registry.start("k", "Label", lambda: "ok"))
        self.wait("k")

    def test_running_reports_live_work(self):
        import threading

        gate = threading.Event()
        self.registry.start("k", "Slow thing", gate.wait)
        try:
            running = self.registry.running()
            self.assertEqual(len(running), 1)
            self.assertEqual(running[0].label, "Slow thing")
            self.assertTrue(running[0].running)
        finally:
            gate.set()
        self.wait("k")

    def test_history_is_capped(self):
        for index in range(tasks.HISTORY_LIMIT + 5):
            key = "k%d" % index
            self.registry.start(key, "L", lambda: "done")
            self.wait(key)
        self.assertLessEqual(len(self.registry.recent(limit=100)),
                             tasks.HISTORY_LIMIT)


class TestActivityPanel(unittest.TestCase):
    """A3: backgrounding is only an improvement if the work stays visible.

    "Started — refresh in a minute" is a broken promise if nothing on the page
    ever shows the job running or how it ended.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)
        db_module.init_db()
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()
        tasks.registry.clear_history()

    def tearDown(self):
        tasks.registry.clear_history()
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def test_no_panel_when_nothing_has_run(self):
        self.assertNotIn("Activity",
                         self.client.get("/").get_data(as_text=True))

    def test_a_running_task_is_shown_on_every_page(self):
        import threading

        gate = threading.Event()
        tasks.registry.start("cycle", "Fetch, score and notify", gate.wait)
        try:
            for path in ("/", "/applications", "/sources"):
                page = self.client.get(path).get_data(as_text=True)
                self.assertIn("Fetch, score and notify", page, path)
                self.assertIn("running", page, path)
        finally:
            gate.set()

    def test_a_finished_task_reports_its_outcome(self):
        tasks.registry.start("score", "Score outstanding postings",
                             lambda: "12 scored")
        for _ in range(300):
            if not tasks.registry.is_running("score"):
                break
            time.sleep(0.01)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Score outstanding postings", page)
        self.assertIn("12 scored", page)


class TestAddSourceRoute(unittest.TestCase):
    """A2: the add-source form, and what it must refuse to add.

    The form exists to register per-company career boards. Letting it also add
    an aggregator was a live bug: a second `arbeitnow` row sailed past the
    UNIQUE name constraint (`arbeitnow` vs the installed `Arbeitnow`) and would
    have fetched the same feed twice on every cycle.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)
        db_module.init_db()
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def adapters_in_the_form(self):
        page = self.client.get("/sources").get_data(as_text=True)
        return re.findall(r'<option value="([^"]+)">', page)

    def add(self, **fields):
        return self.client.post("/sources/add", data=fields,
                                follow_redirects=True)

    def sources_named(self, adapter):
        conn = db_module.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM sources WHERE adapter = ?",
                                (adapter,)).fetchone()[0]
        finally:
            conn.close()

    def test_the_form_offers_only_company_boards(self):
        offered = self.adapters_in_the_form()
        self.assertEqual(sorted(offered),
                         ["greenhouse", "lever", "personio", "recruitee",
                          "smartrecruiters"])

    def test_a_company_board_is_added(self):
        before = self.sources_named("greenhouse")
        page = self.add(adapter="greenhouse", slug="gitlab", company="GitLab")
        self.assertIn("Added", page.get_data(as_text=True))
        self.assertEqual(self.sources_named("greenhouse"), before + 1)

    def test_an_aggregator_cannot_be_added_a_second_time(self):
        before = self.sources_named("arbeitnow")
        page = self.add(adapter="arbeitnow")
        self.assertIn("cannot be added again", page.get_data(as_text=True))
        self.assertEqual(self.sources_named("arbeitnow"), before)

    def test_manual_cannot_be_added_a_second_time(self):
        before = self.sources_named("manual")
        self.add(adapter="manual")
        self.assertEqual(self.sources_named("manual"), before)

    def test_a_board_without_a_slug_is_refused(self):
        before = self.sources_named("lever")
        page = self.add(adapter="lever", slug="   ")
        self.assertIn("needs a company slug", page.get_data(as_text=True))
        self.assertEqual(self.sources_named("lever"), before)

    def test_an_unknown_adapter_is_refused(self):
        page = self.add(adapter="linkedin", slug="whatever")
        self.assertIn("Unknown source type", page.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
