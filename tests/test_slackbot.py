"""Slack cards, the once-ever posting guard, and the tracker writes."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from jobhunt import config, slackbot, tasks, tracker
from jobhunt import db as db_module


class FakeClient(object):
    """Records calls instead of talking to Slack."""

    def __init__(self, fail_on=None):
        self.posted = []
        self.updated = []
        self.fail_on = fail_on
        self._ts = 1000

    def chat_postMessage(self, channel, text=None, blocks=None, **kwargs):
        if self.fail_on is not None and len(self.posted) == self.fail_on:
            raise RuntimeError("slack is down")
        self._ts += 1
        self.posted.append({"channel": channel, "text": text, "blocks": blocks})
        return {"channel": channel, "ts": "{0}.0001".format(self._ts)}

    def chat_update(self, channel, ts, text=None, blocks=None, **kwargs):
        self.updated.append({"channel": channel, "ts": ts, "blocks": blocks})
        return {"ok": True}


class SlackTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, last_status) "
            "VALUES (1, 'Arbeitnow', 'stub', '{}', 1, 'ok')"
        )
        config.set_setting(self.conn, "slack.channel_id", "C123")
        config.set_setting(self.conn, "slack.allowed_user_ids", ["U_ME"])
        config.set_secret(self.conn, "slack.bot_token", "xoxb-test")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()

    def add_scored_job(self, job_id, score=70, title="Support Ops Manager",
                       english=1, german="not_stated", quote="",
                       salary=0, salary_quote="", status="new", stage0=1):
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, title, "
            "company, location, remote, posted_at, first_seen_at, last_seen_at, "
            "description_text, dedup_key, status, stage0_pass) "
            "VALUES (?,1,?,?,?,?,'ACME GmbH','50667 Köln',1,"
            "'2026-08-05T00:00:00+00:00','2026-08-05T00:00:00+00:00',"
            "'2026-08-05T00:00:00+00:00','desc',?,?,?)",
            (job_id, "e%s" % job_id, "https://x/%s" % job_id, "https://x/%s" % job_id,
             title, "k%s" % job_id, status, stage0),
        )
        verdict = "good" if score >= 60 else "stretch"
        self.conn.execute(
            "INSERT INTO scores (job_id, fit_score, verdict, matched_json, gaps_json, "
            "rationale, model_used, prompt_version, scored_at) "
            "VALUES (?,?,?,'[]',?,'Strong operational match.','stub','v1',"
            "'2026-08-06T00:00:00+00:00')",
            (job_id, score, verdict, json.dumps(["No direct reports"])),
        )
        self.conn.execute(
            "INSERT INTO job_details (job_id, salary_stated, salary_quote, "
            "german_level_required, english_sufficient, language_quote, remote_policy) "
            "VALUES (?,?,?,?,?,?,'hybrid')",
            (job_id, salary, salary_quote, german, english, quote),
        )
        self.conn.commit()


class TestCardRendering(SlackTestCase):
    def card_text(self, job_id):
        job = slackbot._load_card_job(self.conn, job_id)
        return json.dumps(slackbot.build_card(job))

    def test_card_shows_score_and_title(self):
        self.add_scored_job(1, score=72)
        text = self.card_text(1)
        self.assertIn("72/100", text)
        self.assertIn("Support Ops Manager", text)

    def test_english_sufficient_is_reported(self):
        self.add_scored_job(1, english=1)
        self.assertIn("English sufficient", self.card_text(1))

    def test_german_requirement_is_reported_when_stated(self):
        self.add_scored_job(1, english=0, german="C1", quote="Sehr gute Deutschkenntnisse")
        text = self.card_text(1)
        self.assertIn("German C1 required", text)
        self.assertIn("Sehr gute Deutschkenntnisse", text)

    def test_unstated_language_says_so(self):
        self.add_scored_job(1, english=0, german="not_stated")
        self.assertIn("not stated", self.card_text(1))

    def test_unstated_salary_is_never_invented(self):
        self.add_scored_job(1, salary=0)
        self.assertIn("Salary not stated", self.card_text(1))

    def test_stated_salary_is_quoted(self):
        self.add_scored_job(1, salary=1, salary_quote="65.000 - 80.000 EUR")
        self.assertIn("65.000 - 80.000 EUR", self.card_text(1))

    def test_all_three_action_buttons_present(self):
        self.add_scored_job(1)
        text = self.card_text(1)
        for action in (slackbot.ACTION_SHORTLIST, slackbot.ACTION_APPLIED,
                       slackbot.ACTION_DISMISS):
            self.assertIn(action, text)

    def test_button_value_carries_the_job_id(self):
        self.add_scored_job(42)
        job = slackbot._load_card_job(self.conn, 42)
        actions = [b for b in slackbot.build_card(job) if b["type"] == "actions"][0]
        self.assertEqual(actions["elements"][0]["value"], "42")

    def test_markup_in_a_title_is_escaped(self):
        self.add_scored_job(1, title="Dev <script> & Ops")
        text = self.card_text(1)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;", text)


class TestPostingGuard(SlackTestCase):
    def test_posts_only_above_the_threshold(self):
        self.add_scored_job(1, score=75)
        self.add_scored_job(2, score=45)
        client = FakeClient()

        posted, rolled = slackbot.post_new_cards(self.conn, client, threshold=60)

        self.assertEqual(posted, 1)
        self.assertEqual(rolled, 1)

    def test_a_job_is_never_posted_twice(self):
        """The guard that decides whether Slack stays usable."""
        self.add_scored_job(1, score=75)
        client = FakeClient()

        first, _ = slackbot.post_new_cards(self.conn, client, threshold=60)
        second, _ = slackbot.post_new_cards(self.conn, client, threshold=60)
        third, _ = slackbot.post_new_cards(self.conn, client, threshold=60)

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(third, 0)
        cards = [p for p in client.posted if p["blocks"]]
        self.assertEqual(len(cards), 1)

    def test_slack_ts_is_recorded(self):
        self.add_scored_job(1, score=75)
        slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        row = self.conn.execute("SELECT slack_ts, slack_channel FROM jobs WHERE id=1").fetchone()
        self.assertIsNotNone(row["slack_ts"])
        self.assertEqual(row["slack_channel"], "C123")

    def test_a_failed_post_is_retried_next_run(self):
        """A crash mid-post must not silently lose the card."""
        self.add_scored_job(1, score=75)
        failing = FakeClient(fail_on=0)
        posted, _ = slackbot.post_new_cards(self.conn, failing, threshold=60)
        self.assertEqual(posted, 0)

        posted, _ = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        self.assertEqual(posted, 1)

    def test_out_of_region_jobs_are_never_posted(self):
        self.add_scored_job(1, score=90, stage0=0)
        posted, _ = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        self.assertEqual(posted, 0)

    def test_dismissed_jobs_are_never_posted(self):
        self.add_scored_job(1, score=90, status="dismissed")
        posted, _ = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        self.assertEqual(posted, 0)

    def test_max_cards_is_respected(self):
        for i in range(1, 6):
            self.add_scored_job(i, score=70)
        posted, _ = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60, limit=2)
        self.assertEqual(posted, 2)

    def test_no_rollup_line_when_nothing_is_below(self):
        self.add_scored_job(1, score=75)
        client = FakeClient()
        slackbot.post_new_cards(self.conn, client, threshold=60)
        self.assertEqual(len(client.posted), 1)


class TestRollupIsNotRepeated(SlackTestCase):
    """Caught live: the rollup said '+151 others' and would have said it forever."""

    def test_rolled_up_jobs_are_counted_only_once(self):
        self.add_scored_job(1, score=75)
        for i in range(2, 8):
            self.add_scored_job(i, score=40)
        client = FakeClient()

        posted, rolled = slackbot.post_new_cards(self.conn, client, threshold=60)
        self.assertEqual((posted, rolled), (1, 6))

        posted2, rolled2 = slackbot.post_new_cards(self.conn, client, threshold=60)
        self.assertEqual((posted2, rolled2), (0, 0), "nothing may be re-announced")

    def test_only_new_jobs_appear_in_the_next_rollup(self):
        for i in range(1, 4):
            self.add_scored_job(i, score=40)
        client = FakeClient()
        slackbot.post_new_cards(self.conn, client, threshold=60)

        self.add_scored_job(99, score=40)
        _, rolled = slackbot.post_new_cards(self.conn, client, threshold=60)
        self.assertEqual(rolled, 1)

    def test_a_failed_rollup_is_retried_rather_than_swallowing_jobs(self):
        self.add_scored_job(1, score=40)
        self.add_scored_job(2, score=40)
        failing = FakeClient(fail_on=0)     # the rollup is the first post here

        _, rolled = slackbot.post_new_cards(self.conn, failing, threshold=60)
        self.assertEqual(rolled, 2)
        unnotified = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE notified_at IS NULL").fetchone()[0]
        self.assertEqual(unnotified, 2, "a failed rollup must not mark jobs done")

        _, rolled2 = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        self.assertEqual(rolled2, 2)

    def test_carded_jobs_are_not_also_rolled_up(self):
        self.add_scored_job(1, score=75)
        self.add_scored_job(2, score=40)
        posted, rolled = slackbot.post_new_cards(self.conn, FakeClient(), threshold=60)
        self.assertEqual((posted, rolled), (1, 1))
        row = self.conn.execute("SELECT notified_at FROM jobs WHERE id=1").fetchone()
        self.assertIsNotNone(row["notified_at"])


class TestAuthorisation(SlackTestCase):
    def test_configured_user_is_allowed(self):
        self.assertTrue(slackbot._is_allowed(self.conn, "U_ME"))

    def test_other_workspace_members_are_not(self):
        self.assertFalse(slackbot._is_allowed(self.conn, "U_SOMEONE_ELSE"))

    def test_empty_allowlist_permits_nobody(self):
        config.set_setting(self.conn, "slack.allowed_user_ids", [])
        self.assertFalse(slackbot._is_allowed(self.conn, "U_ME"))


class TestTrackerWrites(SlackTestCase):
    def test_shortlist_creates_a_pipeline_row(self):
        self.add_scored_job(1)
        tracker.shortlist(self.conn, 1)
        row = tracker.get(self.conn, 1)
        self.assertEqual(row["pipeline_status"], "shortlisted")
        self.assertIsNotNone(row["next_action_date"])
        job = self.conn.execute("SELECT status FROM jobs WHERE id=1").fetchone()
        self.assertEqual(job["status"], "shortlisted")

    def test_mark_applied_sets_a_date_and_follow_up(self):
        self.add_scored_job(1)
        tracker.mark_applied(self.conn, 1)
        row = tracker.get(self.conn, 1)
        self.assertEqual(row["pipeline_status"], "applied")
        self.assertIsNotNone(row["applied_at"])
        self.assertIsNotNone(row["next_action_date"])

    def test_applied_after_shortlist_keeps_the_shortlist_date(self):
        self.add_scored_job(1)
        tracker.shortlist(self.conn, 1)
        first = tracker.get(self.conn, 1)["shortlisted_at"]
        tracker.mark_applied(self.conn, 1)
        self.assertEqual(tracker.get(self.conn, 1)["shortlisted_at"], first)

    def test_shortlist_never_demotes_an_application(self):
        self.add_scored_job(1)
        tracker.mark_applied(self.conn, 1)
        tracker.shortlist(self.conn, 1)
        self.assertEqual(tracker.get(self.conn, 1)["pipeline_status"], "applied")

    def test_dismiss_marks_the_job_not_the_application(self):
        self.add_scored_job(1)
        tracker.dismiss(self.conn, 1, "wrong field")
        job = self.conn.execute(
            "SELECT status, dismissed_reason FROM jobs WHERE id=1").fetchone()
        self.assertEqual(job["status"], "dismissed")
        self.assertEqual(job["dismissed_reason"], "wrong field")

    def test_stale_shortlists_are_found(self):
        self.add_scored_job(1)
        tracker.shortlist(self.conn, 1)
        self.conn.execute(
            "UPDATE applications SET shortlisted_at = '2026-07-01T00:00:00+00:00' "
            "WHERE job_id = 1")
        self.conn.commit()
        self.assertEqual(len(tracker.stale_shortlists(self.conn)), 1)

    def test_recent_shortlists_are_not_stale(self):
        self.add_scored_job(1)
        tracker.shortlist(self.conn, 1)
        self.assertEqual(tracker.stale_shortlists(self.conn), [])


class TestSlashCommands(SlackTestCase):
    """`U_ME` is the allowlisted user; `U_THEM` stands in for the rest of the
    workspace, which shares the public `#job-hunt` channel."""

    def reply(self, text, user="U_ME"):
        return slackbot._command_response(self.conn, text, user)

    def test_help_lists_the_subcommands(self):
        text = self.reply("")
        self.assertIn("status", text)
        self.assertIn("top", text)

    def test_help_advertises_run(self):
        """A9: the manifest promised `run`; help must not contradict it."""
        self.assertIn("run", self.reply("help"))

    def test_status_reports_the_pipeline(self):
        self.add_scored_job(1)
        tracker.shortlist(self.conn, 1)
        self.assertIn("shortlisted", self.reply("status"))

    def test_top_lists_scored_matches(self):
        self.add_scored_job(1, score=75, title="Zendesk Platform Owner")
        self.assertIn("Zendesk Platform Owner", self.reply("top"))

    def test_titles_lists_enabled_search_terms(self):
        self.assertIn("Zendesk", self.reply("titles"))

    def test_unknown_subcommand_is_handled(self):
        self.assertIn("Unknown", self.reply("wat"))

    def test_help_is_open_to_everyone(self):
        """Nothing private in it, and a bare `/jobhunt` should explain itself."""
        self.assertIn("status", self.reply("", user="U_THEM"))

    def test_someone_else_cannot_read_the_pipeline(self):
        self.add_scored_job(1, score=75, title="Zendesk Platform Owner")
        for command in ("status", "top", "titles", "run"):
            self.assertIn("allowlist", self.reply(command, user="U_THEM"),
                          command)

    def test_a_missing_user_id_is_not_allowlisted(self):
        self.assertIn("allowlist",
                      slackbot._command_response(self.conn, "status", None))


class TestRunCommand(SlackTestCase):
    """A9: `run` was advertised in the manifest and answered "Unknown command"."""

    def setUp(self):
        super(TestRunCommand, self).setUp()
        self.registry = tasks.registry
        self.registry.clear_history()

    def test_run_starts_a_cycle_and_says_so(self):
        calls = []

        def fake_cycle(**kwargs):
            calls.append(kwargs)
            return type("R", (), {"summary": lambda self: "1 found"})()

        with mock.patch("jobhunt.scheduler.run_cycle", fake_cycle):
            reply = slackbot._command_response(self.conn, "run", "U_ME")
            for _ in range(300):
                if not self.registry.is_running("cycle"):
                    break
                time.sleep(0.01)

        self.assertIn("Started", reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["trigger"], "slack")

    def test_run_shares_the_cycle_key_with_the_web_buttons(self):
        """Two cycles at once would double-fetch and post duplicate cards."""
        gate = threading.Event()
        self.registry.start("cycle", "Fetch, score and notify", gate.wait)
        try:
            reply = slackbot._command_response(self.conn, "run", "U_ME")
            self.assertIn("already running", reply)
        finally:
            gate.set()
            for _ in range(300):
                if not self.registry.is_running("cycle"):
                    break
                time.sleep(0.01)


class TestNotConfigured(SlackTestCase):
    def test_missing_channel_raises_clearly(self):
        config.set_setting(self.conn, "slack.channel_id", "")
        with self.assertRaises(slackbot.SlackNotConfigured):
            slackbot._require(self.conn)


if __name__ == "__main__":
    unittest.main()
