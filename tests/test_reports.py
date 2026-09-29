"""The daily report — what it surfaces, and what it must never invent."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from jobhunt import config, reports, tracker
from jobhunt import db as db_module

TODAY = date.today().isoformat()


class FakeClient(object):
    def __init__(self):
        self.posted = []

    def chat_postMessage(self, channel, text=None, blocks=None, **kwargs):
        self.posted.append({"channel": channel, "text": text, "blocks": blocks})
        return {"channel": channel, "ts": "1.0"}


class ReportTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, last_status) "
            "VALUES (1, 'Arbeitnow', 'stub', '{}', 1, 'ok')")
        config.set_setting(self.conn, "slack.channel_id", "C123")
        config.set_secret(self.conn, "slack.bot_token", "xoxb-test")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()

    def add_job(self, job_id, score=70, scored_at=None, title="Support Ops Manager"):
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, title, "
            "company, location, first_seen_at, last_seen_at, dedup_key, status, stage0_pass) "
            "VALUES (?,1,?,?,?,?,'ACME','Köln',?,?,?,'new',1)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id, "https://x/%s" % job_id,
             title, TODAY + "T08:00:00+00:00", TODAY + "T08:00:00+00:00", "k%s" % job_id))
        self.conn.execute(
            "INSERT INTO scores (job_id, fit_score, verdict, matched_json, gaps_json, "
            "rationale, model_used, prompt_version, scored_at) "
            "VALUES (?,?,'good','[]','[]','Good match.','stub','v1',?)",
            (job_id, score, scored_at or (TODAY + "T09:00:00+00:00")))
        self.conn.commit()


class TestGather(ReportTestCase):
    def test_counts_todays_scores(self):
        self.add_job(1)
        self.add_job(2, scored_at="2026-01-01T00:00:00+00:00")
        self.assertEqual(reports.gather(self.conn, TODAY)["new_scored"], 1)

    def test_top_lists_only_untouched_above_threshold(self):
        self.add_job(1, score=75)
        self.add_job(2, score=30)
        self.add_job(3, score=80)
        self.conn.execute("UPDATE jobs SET status='shortlisted' WHERE id=3")
        self.conn.commit()
        top = reports.gather(self.conn, TODAY)["top"]
        self.assertEqual([r["id"] for r in top], [1])

    def test_counts_todays_actions(self):
        self.add_job(1)
        tracker.shortlist(self.conn, 1)
        tracker.mark_applied(self.conn, 1)
        data = reports.gather(self.conn, TODAY)
        self.assertEqual(data["applied_today"], 1)

    def test_surfaces_failing_sources(self):
        self.conn.execute(
            "UPDATE sources SET last_status='error', last_error='HTTP 503' WHERE id=1")
        self.conn.commit()
        self.assertEqual(len(reports.gather(self.conn, TODAY)["failing_sources"]), 1)

    def test_finds_due_follow_ups(self):
        self.add_job(1)
        tracker.shortlist(self.conn, 1)
        past = (date.today() - timedelta(days=1)).isoformat()
        self.conn.execute(
            "UPDATE applications SET next_action_date=? WHERE job_id=1", (past,))
        self.conn.commit()
        self.assertEqual(len(reports.gather(self.conn, TODAY)["due"]), 1)

    def test_finds_stale_shortlists(self):
        self.add_job(1)
        tracker.shortlist(self.conn, 1)
        old = (date.today() - timedelta(days=20)).isoformat() + "T00:00:00+00:00"
        self.conn.execute(
            "UPDATE applications SET shortlisted_at=? WHERE job_id=1", (old,))
        self.conn.commit()
        self.assertEqual(len(reports.gather(self.conn, TODAY)["stale"]), 1)


class TestRender(ReportTestCase):
    def test_quiet_day_says_so_rather_than_rendering_empty(self):
        blocks = reports.render(reports.gather(self.conn, TODAY))
        self.assertIn("Nothing new today", json.dumps(blocks))

    def test_top_matches_are_listed_with_links(self):
        self.add_job(1, score=75, title="Zendesk Platform Owner")
        text = json.dumps(reports.render(reports.gather(self.conn, TODAY)))
        self.assertIn("Zendesk Platform Owner", text)
        self.assertIn("https://x/1", text)

    def test_stale_shortlists_are_called_out(self):
        self.add_job(1)
        tracker.shortlist(self.conn, 1)
        old = (date.today() - timedelta(days=20)).isoformat() + "T00:00:00+00:00"
        self.conn.execute(
            "UPDATE applications SET shortlisted_at=? WHERE job_id=1", (old,))
        self.conn.commit()
        text = json.dumps(reports.render(reports.gather(self.conn, TODAY)))
        self.assertIn("Shortlisted but not applied", text)

    def test_failing_sources_are_flagged(self):
        self.conn.execute(
            "UPDATE sources SET last_status='error', last_error='HTTP 503' WHERE id=1")
        self.conn.commit()
        text = json.dumps(reports.render(reports.gather(self.conn, TODAY)))
        self.assertIn("Sources failing", text)

    def test_markup_is_escaped(self):
        self.add_job(1, score=75, title="Ops <b>lead</b> & co")
        text = json.dumps(reports.render(reports.gather(self.conn, TODAY)))
        self.assertIn("&lt;b&gt;", text)


class TestPost(ReportTestCase):
    def test_posts_to_the_configured_channel(self):
        self.add_job(1, score=75)
        client = FakeClient()
        reports.post_daily_report(self.conn, client, TODAY)
        self.assertEqual(len(client.posted), 1)
        self.assertEqual(client.posted[0]["channel"], "C123")

    def test_skips_cleanly_when_slack_is_unconfigured(self):
        config.set_setting(self.conn, "slack.channel_id", "")
        self.assertIsNone(reports.post_daily_report(self.conn, FakeClient(), TODAY))


class TestDayBoundary(ReportTestCase):
    """A10: the report's day is a Berlin day, not a UTC one.

    Berlin is UTC+2 in summer, so Berlin midnight is 22:00 UTC the day before.
    The old code used `T00:00:00+00:00`, which pushed the window two hours late
    and dropped anything scored between midnight and 02:00 Berlin.
    """

    def test_berlin_midnight_is_2200_utc_the_previous_day_in_summer(self):
        self.assertEqual(reports.day_start_utc(self.conn, "2026-07-15"),
                         "2026-07-14T22:00:00+00:00")

    def test_berlin_midnight_is_2300_utc_the_previous_day_in_winter(self):
        """CET, not CEST — the offset is not a constant."""
        self.assertEqual(reports.day_start_utc(self.conn, "2026-01-15"),
                         "2026-01-14T23:00:00+00:00")

    def test_the_configured_timezone_is_honoured(self):
        config.set_setting(self.conn, "schedule.timezone", "UTC")
        self.conn.commit()
        self.assertEqual(reports.day_start_utc(self.conn, "2026-07-15"),
                         "2026-07-15T00:00:00+00:00")

    def test_work_just_after_berlin_midnight_counts_for_that_day(self):
        """The bug: 00:30 Berlin = 22:30 UTC, which the old window excluded."""
        self.add_job(1, scored_at="2026-07-14T22:30:00+00:00")
        self.assertEqual(
            reports.gather(self.conn, "2026-07-15")["new_scored"], 1)

    def test_work_just_before_berlin_midnight_belongs_to_the_day_before(self):
        self.add_job(1, scored_at="2026-07-14T21:30:00+00:00")
        self.assertEqual(
            reports.gather(self.conn, "2026-07-15")["new_scored"], 0)


if __name__ == "__main__":
    unittest.main()
