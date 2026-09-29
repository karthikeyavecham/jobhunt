"""Ingest behaviour, especially under repetition.

The 3-hour cycle re-sees a long-lived posting ~70 times. These tests pin down
that repeated fetches insert nothing new, and that the same role arriving from
a second source merges into one row rather than producing a second card.
"""

import json
import tempfile
import unittest
from pathlib import Path

from jobhunt import db as db_module
from jobhunt import ingest
from jobhunt.sources.base import RawJob, SourceError


def make_job(external_id="j1", title="Zendesk Platform Owner (m/w/d)",
             company="FREE NOW GmbH", location="50667 Köln", url=None,
             description="Own the Zendesk platform. Fluent English required."):
    return RawJob(
        external_id=external_id,
        url=url or "https://example.com/jobs/{0}".format(external_id),
        title=title,
        company=company,
        location=location,
        remote=False,
        posted_at="2026-08-01T09:00:00+00:00",
        description_html="<p>{0}</p>".format(description),
        description_text=description,
        raw={"slug": external_id},
    )


class StubAdapter(object):
    """Returns a fixed list, so tests never touch the network."""

    def __init__(self, jobs, fail_with=None):
        self.jobs = jobs
        self.fail_with = fail_with
        self.calls = 0

    def fetch(self, config):
        self.calls += 1
        if self.fail_with:
            raise SourceError(self.fail_with)
        return self.jobs


class IngestTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "test.db"
        db_module.init_db()
        self.conn = db_module.connect()
        # init_db seeds real sources and the default job titles; clear both so
        # each test starts from a known, empty state.
        self.conn.execute("DELETE FROM sources")
        self.conn.execute("DELETE FROM search_terms")
        self.conn.commit()

        self._orig_get_adapter = ingest.get_adapter
        self._adapters = {}
        ingest.get_adapter = lambda name: self._adapters[name]

    def tearDown(self):
        ingest.get_adapter = self._orig_get_adapter
        self.conn.close()
        db_module.DB_PATH = self._orig_path
        self._tmp.cleanup()

    def add_source(self, name, adapter_name, adapter):
        self._adapters[adapter_name] = adapter
        self.conn.execute(
            "INSERT INTO sources (name, adapter, config_json, enabled, last_status) "
            "VALUES (?, ?, '{}', 1, 'never')",
            (name, adapter_name),
        )
        self.conn.commit()

    def job_rows(self):
        return self.conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()


class TestRepeatedFetch(IngestTestCase):
    def test_five_fetches_insert_once(self):
        """The decisive test: 5 runs of identical data must yield 1 job row."""
        self.add_source("Stub", "stub", StubAdapter([make_job()]))

        inserted_total = 0
        for _ in range(5):
            result = ingest.run_fetch(trigger="manual", conn=self.conn)
            inserted_total += result.inserted

        self.assertEqual(inserted_total, 1, "only the first run may insert")
        self.assertEqual(len(self.job_rows()), 1)

    def test_repeat_bumps_last_seen_without_touching_first_seen(self):
        self.add_source("Stub", "stub", StubAdapter([make_job()]))
        ingest.run_fetch(conn=self.conn)
        first = self.job_rows()[0]
        ingest.run_fetch(conn=self.conn)
        second = self.job_rows()[0]
        self.assertEqual(first["first_seen_at"], second["first_seen_at"])

    def test_new_job_ids_only_reported_once(self):
        """Slack cards are driven off new_job_ids — it must be empty on re-runs."""
        self.add_source("Stub", "stub", StubAdapter([make_job()]))
        first = ingest.run_fetch(conn=self.conn)
        second = ingest.run_fetch(conn=self.conn)
        self.assertEqual(len(first.new_job_ids), 1)
        self.assertEqual(second.new_job_ids, [])


class TestCrossSourceMerge(IngestTestCase):
    def test_same_role_from_two_sources_makes_one_row(self):
        self.add_source("Board A", "a", StubAdapter([
            make_job(external_id="a1", company="FREE NOW GmbH",
                     title="Zendesk Platform Owner (m/w/d)", location="50667 Köln",
                     url="https://a.example/jobs/a1", description="Short version."),
        ]))
        self.add_source("Board B", "b", StubAdapter([
            make_job(external_id="b1", company="FREE NOW",
                     title="Zendesk Platform Owner", location="Koeln, Germany",
                     url="https://b.example/jobs/b1",
                     description="A much longer and more detailed description of the role."),
        ]))

        result = ingest.run_fetch(conn=self.conn)

        self.assertEqual(result.inserted, 1)
        self.assertEqual(result.merged, 1)
        rows = self.job_rows()
        self.assertEqual(len(rows), 1)

    def test_merge_keeps_the_richer_description(self):
        long_text = "A much longer and more detailed description of the role."
        self.add_source("Board A", "a", StubAdapter([
            make_job(external_id="a1", url="https://a.example/1", description="Short."),
        ]))
        self.add_source("Board B", "b", StubAdapter([
            make_job(external_id="b1", url="https://b.example/1", description=long_text),
        ]))
        ingest.run_fetch(conn=self.conn)
        self.assertEqual(self.job_rows()[0]["description_text"], long_text)

    def test_merge_records_the_alternate_url(self):
        self.add_source("Board A", "a", StubAdapter([
            make_job(external_id="a1", url="https://a.example/1"),
        ]))
        self.add_source("Board B", "b", StubAdapter([
            make_job(external_id="b1", url="https://b.example/1"),
        ]))
        ingest.run_fetch(conn=self.conn)
        payload = json.loads(self.job_rows()[0]["raw_json"])
        alt_urls = [a["url"] for a in payload["_alt_sources"]]
        self.assertEqual(alt_urls, ["https://b.example/1"])

    def test_repeated_merge_does_not_duplicate_alternates(self):
        self.add_source("Board A", "a", StubAdapter([make_job(external_id="a1", url="https://a.example/1")]))
        self.add_source("Board B", "b", StubAdapter([make_job(external_id="b1", url="https://b.example/1")]))
        for _ in range(3):
            ingest.run_fetch(conn=self.conn)
        payload = json.loads(self.job_rows()[0]["raw_json"])
        self.assertEqual(len(payload["_alt_sources"]), 1)


class TestContentChange(IngestTestCase):
    def test_changed_description_updates_in_place(self):
        adapter = StubAdapter([make_job(description="Original text.")])
        self.add_source("Stub", "stub", adapter)
        ingest.run_fetch(conn=self.conn)

        adapter.jobs = [make_job(description="Rewritten with new requirements.")]
        result = ingest.run_fetch(conn=self.conn)

        self.assertEqual(result.inserted, 0)
        self.assertEqual(len(self.job_rows()), 1)
        self.assertEqual(
            self.job_rows()[0]["description_text"], "Rewritten with new requirements."
        )


class TestFailureIsolation(IngestTestCase):
    def test_one_broken_source_does_not_stop_the_others(self):
        self.add_source("Broken", "broken", StubAdapter([], fail_with="upstream 503"))
        self.add_source("Working", "working", StubAdapter([make_job(external_id="w1")]))

        result = ingest.run_fetch(conn=self.conn)

        self.assertEqual(result.inserted, 1, "the healthy source must still deliver")
        self.assertEqual(len(result.errors), 1)
        self.assertIn("upstream 503", result.errors[0]["error"])

    def test_failure_is_recorded_on_the_source_row(self):
        self.add_source("Broken", "broken", StubAdapter([], fail_with="upstream 503"))
        ingest.run_fetch(conn=self.conn)
        row = self.conn.execute("SELECT * FROM sources WHERE name = 'Broken'").fetchone()
        self.assertEqual(row["last_status"], "error")
        self.assertIn("upstream 503", row["last_error"])

    def test_source_recovers_and_clears_the_error(self):
        adapter = StubAdapter([], fail_with="upstream 503")
        self.add_source("Flaky", "flaky", adapter)
        ingest.run_fetch(conn=self.conn)

        adapter.fail_with = None
        adapter.jobs = [make_job(external_id="f1")]
        ingest.run_fetch(conn=self.conn)

        row = self.conn.execute("SELECT * FROM sources WHERE name = 'Flaky'").fetchone()
        self.assertEqual(row["last_status"], "ok")
        self.assertIsNone(row["last_error"])


class DetailAdapter(StubAdapter):
    """Two-step source: search returns summaries, details cost a second request."""

    needs_detail_fetch = True

    def __init__(self, jobs):
        StubAdapter.__init__(self, jobs)
        self.detail_calls = []

    def fetch_detail(self, job, config):
        self.detail_calls.append(job.external_id)
        job.description_text = "Full description for {0}".format(job.external_id)
        return job


class TestDetailFetching(IngestTestCase):
    """Detail requests are the expensive part; they must only fire once per job."""

    def test_detail_is_fetched_for_new_jobs(self):
        adapter = DetailAdapter([make_job(external_id="d1")])
        self.add_source("Detail", "detail", adapter)
        ingest.run_fetch(conn=self.conn)
        self.assertEqual(adapter.detail_calls, ["d1"])
        self.assertEqual(
            self.job_rows()[0]["description_text"], "Full description for d1"
        )

    def test_detail_is_not_refetched_on_later_runs(self):
        adapter = DetailAdapter([make_job(external_id="d1")])
        self.add_source("Detail", "detail", adapter)
        for _ in range(5):
            ingest.run_fetch(conn=self.conn)
        self.assertEqual(
            adapter.detail_calls, ["d1"],
            "5 runs must cost exactly 1 detail request, not 5",
        )

    def test_detail_is_skipped_for_cross_source_duplicates(self):
        self.add_source("Board A", "a", StubAdapter([
            make_job(external_id="a1", url="https://a.example/1",
                     description="Already has a description."),
        ]))
        detail = DetailAdapter([make_job(external_id="b1", url="https://b.example/1")])
        self.add_source("Board B", "detail", detail)

        ingest.run_fetch(conn=self.conn)

        self.assertEqual(detail.detail_calls, [],
                         "a duplicate is not worth a detail request")

    def test_detail_failure_stores_the_summary_anyway(self):
        class Broken(DetailAdapter):
            def fetch_detail(self, job, config):
                raise RuntimeError("detail endpoint down")

        self.add_source("Detail", "detail", Broken([make_job(external_id="d1")]))
        result = ingest.run_fetch(conn=self.conn)

        self.assertEqual(result.inserted, 1, "the posting is still worth keeping")
        self.assertEqual(len(self.job_rows()), 1)


class TestSearchTermInjection(IngestTestCase):
    def test_enabled_terms_reach_the_adapter(self):
        captured = {}

        class Capturing(StubAdapter):
            def fetch(self, config):
                captured["terms"] = config.get("_search_terms")
                return []

        self.conn.execute(
            "INSERT INTO search_terms (term, enabled, match_scope, created_at) "
            "VALUES ('Zendesk Platform Owner', 1, 'title', '2026-08-05T00:00:00+00:00')"
        )
        self.conn.execute(
            "INSERT INTO search_terms (term, enabled, match_scope, created_at) "
            "VALUES ('Disabled Term', 0, 'title', '2026-08-05T00:00:00+00:00')"
        )
        self.conn.commit()
        self.add_source("Cap", "cap", Capturing([]))

        ingest.run_fetch(conn=self.conn)

        self.assertEqual(captured["terms"], ["Zendesk Platform Owner"])


class TestRunLog(IngestTestCase):
    def test_run_is_recorded_with_counts(self):
        self.add_source("Stub", "stub", StubAdapter([make_job(), make_job(external_id="j2", title="Support Ops Lead")]))
        ingest.run_fetch(trigger="schedule", conn=self.conn)
        run = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(run["trigger"], "schedule")
        self.assertEqual(run["jobs_found"], 2)
        self.assertEqual(run["jobs_new"], 2)
        self.assertIsNotNone(run["finished_at"])


if __name__ == "__main__":
    unittest.main()
