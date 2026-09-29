"""Generation guarantees: colour honesty, confidentiality, provider routing."""

import tempfile
import unittest
from pathlib import Path

from jobhunt import config, generation, llm, prompts
from jobhunt import db as db_module


def full_cv_plan(**overrides):
    plan = {
        "brand_color": "#1B4F8A",
        "brand_color_recognised": False,
        "brand_color_reason": "Not recognised.",
        "headline": "Support Operations Manager",
        "summary": "Twelve years in support operations.",
        "core_strengths": [{"label": "Zendesk", "value": "Full admin"}],
        "roles": [{
            "title": "Support Operations Manager", "company": "Acme SE",
            "dates": "Apr 2021 – Present", "location": "Cologne",
            "groups": [{"heading": "Platform", "bullets": ["Ran the Zendesk estate."]}],
        }],
        "education": ["B.Tech CSE"],
        "awards": ["Hackathon winner"],
    }
    plan.update(overrides)
    return plan


class StubProvider(llm.Provider):
    name = "stub"
    model = "stub-1"

    def __init__(self, plan=None):
        self.plan = plan or full_cv_plan()

    def complete_json(self, system, user, json_schema, max_tokens=1500,
                      temperature=0.1):
        self.last_system = system
        self.last_user = user
        return dict(self.plan)


class GenerationTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        db_module.init_db()
        self.conn = db_module.connect()
        self.conn.execute("DELETE FROM sources")
        self.conn.execute(
            "INSERT INTO sources (id, name, adapter, config_json, enabled, last_status) "
            "VALUES (1,'S','stub','{}',1,'ok')")
        self.conn.execute(
            "INSERT INTO jobs (id, source_id, external_id, url, canonical_url, title, "
            "company, location, first_seen_at, last_seen_at, description_text, "
            "dedup_key, status, stage0_pass) VALUES "
            "(1,1,'e1','https://x/1','https://x/1','Support Agent','Agraya GmbH',"
            "'Köln','2026-08-06T00:00:00+00:00','2026-08-06T00:00:00+00:00',"
            "'Own the support desk.','k1','new',1)")
        self.conn.commit()
        self.job = self.conn.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        self.outdir = Path(self._tmp.name) / "docs"

    def tearDown(self):
        self.conn.close()
        db_module.DB_PATH = self._orig
        self._tmp.cleanup()

    def generate(self, provider=None, **kwargs):
        return generation.generate_document(
            self.conn, self.job, provider or StubProvider(),
            output_dir=self.outdir, **kwargs)


class TestBrandColourHonesty(GenerationTestCase):
    """Observed live: the model justified the fallback navy as 'matching
    Agraya's branding' — a company whose palette it cannot know."""

    def test_unrecognised_brand_reverts_to_navy(self):
        provider = StubProvider(full_cv_plan(
            brand_color="#FF6600", brand_color_recognised=False))
        result = self.generate(provider)
        self.assertEqual(result["brand_color"], "#1B4F8A")

    def test_recognised_brand_is_kept(self):
        provider = StubProvider(full_cv_plan(
            brand_color="#E2001A", brand_color_recognised=True))
        self.assertEqual(self.generate(provider)["brand_color"], "#E2001A")

    def test_an_explicit_override_beats_the_suggestion(self):
        provider = StubProvider(full_cv_plan(
            brand_color="#E2001A", brand_color_recognised=True))
        result = self.generate(provider, brand="#2E7D32")
        self.assertEqual(result["brand_color"], "#2E7D32")

    def test_a_reason_is_always_present(self):
        provider = StubProvider(full_cv_plan(
            brand_color_recognised=False, brand_color_reason=""))
        result = self.generate(provider)
        self.assertIn("not recognised", result["brand_color_reason"].lower())

    def test_malformed_hex_falls_back(self):
        provider = StubProvider(full_cv_plan(
            brand_color="octarine", brand_color_recognised=True))
        self.assertEqual(self.generate(provider)["brand_color"], "#1B4F8A")


class TestConfidentialityBlocking(GenerationTestCase):
    def test_a_blocked_term_stops_generation(self):
        plan = full_cv_plan()
        plan["roles"][0]["groups"][0]["bullets"] = ["Led BotConnect end to end"]
        with self.assertRaises(generation.GenerationBlocked) as ctx:
            self.generate(StubProvider(plan))
        self.assertIn("BotConnect", ctx.exception.terms)

    def test_nothing_is_written_when_blocked(self):
        plan = full_cv_plan()
        plan["summary"] = "Delivered BotConnect."
        with self.assertRaises(generation.GenerationBlocked):
            self.generate(StubProvider(plan))
        self.assertFalse(self.outdir.exists() and any(self.outdir.iterdir()))

    def test_no_document_row_is_recorded_when_blocked(self):
        plan = full_cv_plan()
        plan["summary"] = "Delivered BotConnect."
        with self.assertRaises(generation.GenerationBlocked):
            self.generate(StubProvider(plan))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_clean_content_generates(self):
        self.assertTrue(self.generate()["docx"].exists())


class TestVersioning(GenerationTestCase):
    def test_versions_increment_per_kind(self):
        self.assertEqual(self.generate()["version"], 1)
        self.assertEqual(self.generate()["version"], 2)

    def test_a_row_is_recorded(self):
        self.generate()
        row = self.conn.execute("SELECT * FROM documents").fetchone()
        self.assertEqual(row["kind"], "cv")
        self.assertEqual(row["format"], "docx")
        self.assertEqual(row["model_used"], "stub:stub-1")

    def test_filename_carries_company_and_role(self):
        name = self.generate()["docx"].name
        self.assertIn("Agraya", name)
        self.assertIn("Support_Agent", name)


class TestPromptSafety(GenerationTestCase):
    def test_the_posting_is_delimited_as_data(self):
        provider = StubProvider()
        self.generate(provider)
        self.assertIn("=== JOB POSTING (data) ===", provider.last_user)

    def sent_system_prompt(self):
        """The prompt the provider actually received, not a module constant.

        Asserting on the constant missed the composition: the rules live in
        their own editable block now, and what matters is that they arrive.
        """
        provider = StubProvider()
        self.generate(provider)
        return provider.last_system

    def test_the_system_prompt_forbids_internal_names(self):
        self.assertIn("Never name internal projects", self.sent_system_prompt())

    def test_the_system_prompt_forbids_stating_a_language_level(self):
        """The level is rendered from the profile afterwards, so anything the
        model writes there is discarded — it must not try."""
        self.assertIn("never state a language level", self.sent_system_prompt())

    def test_the_system_prompt_suppresses_the_departure_date(self):
        """The profile states it so the model knows the role is current; a CV
        that advertises a notice period is a worse CV."""
        self.assertIn("notice period", self.sent_system_prompt())

    def test_the_letter_prompt_forbids_claiming_direct_reports(self):
        self.assertIn("never had direct reports",
                      prompts.system_prompt(self.conn, "letter"))

    def test_the_rules_are_sent_once_not_twice(self):
        """The defaults already end with the rules; appending them again would
        send the same constraints twice."""
        sent = self.sent_system_prompt()
        self.assertEqual(sent.count("Hard constraints, non-negotiable:"), 1)


class TestWritingProviderRouting(unittest.TestCase):
    """Scoring runs locally; writing prefers Claude when a key exists."""

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

    def test_without_a_key_writing_stays_local(self):
        provider = config.build_writing_provider(self.conn)
        self.assertIsInstance(provider, llm.OpenAICompatProvider)

    def test_with_a_key_writing_leads_with_claude(self):
        config.set_secret(self.conn, "anthropic.api_key", "sk-ant-test")
        provider = config.build_writing_provider(self.conn)
        self.assertIsInstance(provider, llm.FallbackProvider)
        self.assertIsInstance(provider.primary, llm.AnthropicProvider)
        self.assertIsInstance(provider.secondary, llm.OpenAICompatProvider)

    def test_scoring_keeps_the_opposite_preference(self):
        config.set_secret(self.conn, "anthropic.api_key", "sk-ant-test")
        provider = config.build_provider(self.conn)
        self.assertIsInstance(provider.primary, llm.OpenAICompatProvider)
        self.assertIsInstance(provider.secondary, llm.AnthropicProvider)


if __name__ == "__main__":
    unittest.main()


class TestAssessmentReachesTheWriter(GenerationTestCase):
    """Regression: CARD_SQL once omitted matched_json, so every CV generated
    via Slack or the web silently lost its 'strengths to lead with' input."""

    def add_score(self):
        self.conn.execute(
            "INSERT INTO scores (job_id, fit_score, verdict, matched_json, "
            "gaps_json, rationale, model_used, prompt_version, scored_at) "
            "VALUES (1, 75, 'good', '[\"Zendesk end-to-end ownership\"]', "
            "'[\"No direct reports\"]', 'Good match.', 'stub', 'v1', "
            "'2026-08-07T00:00:00+00:00')")
        self.conn.commit()

    def card_row(self):
        from jobhunt import slackbot
        return self.conn.execute(slackbot.CARD_SQL + "WHERE j.id = 1").fetchone()

    def test_card_sql_carries_both_strengths_and_gaps(self):
        self.add_score()
        row = self.card_row()
        self.assertIn("matched_json", row.keys())
        self.assertIn("gaps_json", row.keys())

    def test_the_prompt_contains_strengths_and_gaps(self):
        self.add_score()
        block = generation._assessment_block(self.card_row())
        self.assertIn("Zendesk end-to-end ownership", block,
                      "strengths must reach the writer")
        self.assertIn("No direct reports", block)


class TestInFlightGuard(unittest.TestCase):
    """Shared double-submit guard for Slack and web generation."""

    def test_second_acquire_is_refused_while_held(self):
        with generation.in_flight_guard(1, "cv") as first:
            self.assertTrue(first)
            with generation.in_flight_guard(1, "cv") as second:
                self.assertFalse(second, "a double-click must be a no-op")

    def test_different_kind_or_job_is_independent(self):
        with generation.in_flight_guard(1, "cv") as first:
            self.assertTrue(first)
            with generation.in_flight_guard(1, "cover_letter") as other_kind:
                self.assertTrue(other_kind)
            with generation.in_flight_guard(2, "cv") as other_job:
                self.assertTrue(other_job)

    def test_released_after_exit(self):
        with generation.in_flight_guard(1, "cv"):
            pass
        with generation.in_flight_guard(1, "cv") as again:
            self.assertTrue(again)

    def test_released_even_when_the_body_raises(self):
        try:
            with generation.in_flight_guard(1, "cv"):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        with generation.in_flight_guard(1, "cv") as again:
            self.assertTrue(again, "an exception must not leak the lock")

    def test_a_losing_context_never_releases_the_winners_claim(self):
        with generation.in_flight_guard(1, "cv") as first:
            self.assertTrue(first)
            with generation.in_flight_guard(1, "cv"):
                pass    # loser exits here — must not free the key
            with generation.in_flight_guard(1, "cv") as third:
                self.assertFalse(third, "the original claim must still hold")
