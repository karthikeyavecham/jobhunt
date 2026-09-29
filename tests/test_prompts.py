"""Editable writing prompts.

The prompt is the main quality lever in this app, so it is editable from the
config page. These pin down the two things that make that safe: an unedited
database behaves exactly as the shipped code says, and a customised one sends
what the page shows.
"""

import os
import tempfile
import unittest
from pathlib import Path

from jobhunt import app as app_module
from jobhunt import config, prompts
from jobhunt import db as db_module


class PromptTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        os.environ.pop("JOBHUNT_PASSWORD", None)
        db_module.init_db()
        self.conn = db_module.connect()
        self.addCleanup(self.conn.close)
        application = app_module.create_app()
        application.config["TESTING"] = True
        self.client = application.test_client()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()


class TestDefaults(PromptTestCase):
    def test_an_untouched_database_uses_the_shipped_wording(self):
        for name in prompts.EDITABLE:
            self.assertEqual(prompts.get(self.conn, name), prompts.default(name))
            self.assertFalse(prompts.is_customised(self.conn, name), name)

    def test_the_cv_prompt_carries_the_rules(self):
        sent = prompts.system_prompt(self.conn, "cv")
        self.assertIn("Hard constraints", sent)
        self.assertIn("Never invent experience", sent)

    def test_the_profile_constraints_reach_the_prompt(self):
        """What must never be overstated belongs to the candidate, not to the
        shipped wording — otherwise the app only works for one person."""
        from jobhunt import profile
        profile.set_section(self.conn, "constraints",
                            "- Dutch is A2. Never imply higher.")
        sent = prompts.system_prompt(self.conn, "cv")
        self.assertIn("Dutch is A2", sent)
        self.assertNotIn("{constraints}", sent)

    def test_an_empty_constraints_section_leaves_no_placeholder(self):
        from jobhunt import profile
        profile.set_section(self.conn, "constraints", "")
        sent = prompts.system_prompt(self.conn, "cv")
        self.assertNotIn("{constraints}", sent)
        self.assertIn("none recorded", sent)

    def test_the_rules_appear_once(self):
        sent = prompts.system_prompt(self.conn, "cv")
        self.assertEqual(sent.count("Hard constraints, non-negotiable:"), 1)

    def test_version_reports_the_shipped_version_until_edited(self):
        self.assertEqual(prompts.version(self.conn), prompts.PROMPT_VERSION)


class TestEditing(PromptTestCase):
    def test_a_custom_prompt_is_what_gets_sent(self):
        prompts.set_prompt(self.conn, "cv_system", "Write a haiku.")
        self.assertIn("Haiku".lower(),
                      prompts.system_prompt(self.conn, "cv").lower())

    def test_custom_text_still_gets_the_rules_appended(self):
        """Editing the CV block must not silently drop the safety rules."""
        prompts.set_prompt(self.conn, "cv_system", "Write a haiku.")
        self.assertIn("Never invent experience",
                      prompts.system_prompt(self.conn, "cv"))

    def test_saving_the_default_back_does_not_freeze_it(self):
        """Storing a copy would stop future improvements reaching him."""
        prompts.set_prompt(self.conn, "cv_system", prompts.default("cv_system"))
        self.assertFalse(prompts.is_customised(self.conn, "cv_system"))
        self.assertEqual(config.get_setting(self.conn, "prompts.cv_system"), "")

    def test_blank_falls_back_to_the_default(self):
        prompts.set_prompt(self.conn, "cv_system", "   ")
        self.assertEqual(prompts.get(self.conn, "cv_system"),
                         prompts.default("cv_system"))

    def test_reset_restores_the_default(self):
        prompts.set_prompt(self.conn, "letter_system", "Something else.")
        self.assertTrue(prompts.is_customised(self.conn, "letter_system"))
        prompts.reset(self.conn, "letter_system")
        self.assertEqual(prompts.get(self.conn, "letter_system"),
                         prompts.default("letter_system"))

    def test_version_becomes_custom_once_edited(self):
        prompts.set_prompt(self.conn, "cv_system", "Different.")
        self.assertEqual(prompts.version(self.conn), "custom")

    def test_editing_one_block_leaves_the_others_alone(self):
        prompts.set_prompt(self.conn, "cv_system", "Different.")
        self.assertFalse(prompts.is_customised(self.conn, "letter_system"))


class TestPromptsPage(PromptTestCase):
    def test_the_page_renders_every_block(self):
        page = self.client.get("/config/prompts").get_data(as_text=True)
        for name in prompts.EDITABLE:
            self.assertIn('name="{0}"'.format(name), page)

    def test_the_config_page_links_to_it(self):
        page = self.client.get("/config").get_data(as_text=True)
        self.assertIn("/config/prompts", page)

    def test_saving_through_the_form_takes_effect(self):
        self.client.post("/config/prompts/save",
                         data={"cv_system": "Write it in limericks."},
                         follow_redirects=True)
        self.assertIn("limericks", prompts.system_prompt(self.conn, "cv"))

    def test_saving_one_block_does_not_blank_the_others(self):
        """The form posts a single textarea; absent fields mean 'unchanged'."""
        prompts.set_prompt(self.conn, "letter_system", "Custom letter.")
        self.client.post("/config/prompts/save",
                         data={"cv_system": "Custom cv."},
                         follow_redirects=True)
        self.assertEqual(prompts.get(self.conn, "letter_system"),
                         "Custom letter.")

    def test_resetting_through_the_form_restores_the_default(self):
        prompts.set_prompt(self.conn, "cv_system", "Custom.")
        self.client.post("/config/prompts/reset", data={"name": "cv_system"},
                         follow_redirects=True)
        self.assertFalse(prompts.is_customised(self.conn, "cv_system"))

    def test_an_unknown_prompt_name_is_refused(self):
        page = self.client.post("/config/prompts/reset",
                                data={"name": "../../etc/passwd"},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn("Unknown prompt", page)

    def test_the_page_shows_the_composed_prompt(self):
        page = self.client.get("/config/prompts").get_data(as_text=True)
        self.assertIn("What actually gets sent", page)
        self.assertIn("Hard constraints", page)

    def test_customisation_is_flagged_on_the_page(self):
        prompts.set_prompt(self.conn, "cv_system", "Custom.")
        page = self.client.get("/config/prompts").get_data(as_text=True)
        self.assertIn("customised", page)


class TestTheLessonsFromTheQwenOutput(PromptTestCase):
    """Each of these encodes a specific defect seen in a real local-model CV,
    compared against the same role written by Claude (2026-08-25)."""

    def cv_prompt(self):
        return prompts.system_prompt(self.conn, "cv")

    def test_core_strengths_are_demanded_not_merely_permitted(self):
        """qwen returned none; the schema allowed it and the prompt never asked."""
        prompt = self.cv_prompt()
        self.assertIn("core_strengths", prompt)
        self.assertIn("never return it empty", prompt)

    def test_the_headline_must_not_echo_the_posting_title(self):
        """qwen produced "(Senior) Business Automation Developer" verbatim."""
        prompt = self.cv_prompt()
        self.assertIn("not a copy of the posting's title", prompt)
        self.assertIn("(m/w/d)", prompt)

    def test_education_is_confined_to_the_education_field(self):
        """qwen emitted the degree as a role, listing the university as an
        employer, and repeated it in education."""
        self.assertIn("Never emit a degree as an entry in roles", self.cv_prompt())

    def test_a_departure_date_constraint_reaches_the_prompt(self):
        """A profile that says "leaving <date>" made the local model put it in
        the summary and the role's date line. The rule now travels with the
        profile rather than being hard-coded for one employer."""
        from jobhunt import profile
        profile.set_section(
            self.conn, "constraints",
            "- Never mention a notice period or departure date.")
        self.assertIn("notice period", self.cv_prompt())

    def test_bullets_are_ordered_by_relevance(self):
        self.assertIn("strongest first", self.cv_prompt())

    def test_repetition_across_sections_is_forbidden(self):
        self.assertIn("Do not state the same fact in two sections",
                      self.cv_prompt())


if __name__ == "__main__":
    unittest.main()
