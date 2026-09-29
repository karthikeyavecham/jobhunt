"""D2: only the production instance owns the scheduler and Slack.

Two live instances would mean two schedulers double-fetching and two Socket
Mode connections splitting button clicks between two databases. The guard is
that owning them is opt-in, so starting the app on the Mac out of habit gives
a read-only web UI rather than a second live instance.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobhunt import app as app_module
from jobhunt import db as db_module


class RoleTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        for name in ("JOBHUNT_ROLE", "JOBHUNT_NO_SLACK", "JOBHUNT_NO_SCHEDULER",
                     "JOBHUNT_PASSWORD"):
            os.environ.pop(name, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def start(self, role=None):
        """Create the app with the given role; report what it tried to start."""
        if role is not None:
            os.environ["JOBHUNT_ROLE"] = role
        with mock.patch("jobhunt.slackbot.start_socket_mode") as slack, \
                mock.patch("jobhunt.scheduler.scheduler.start") as sched:
            app = app_module.create_app()
        return app, slack.called, sched.called


class TestRoleGate(RoleTestCase):
    def test_no_role_starts_neither(self):
        """The default has to be the safe one — habit runs the bare command."""
        app, slack, sched = self.start()
        self.assertFalse(slack)
        self.assertFalse(sched)
        self.assertFalse(app.config["JOBHUNT_PRODUCTION"])

    def test_production_starts_both(self):
        app, slack, sched = self.start("production")
        self.assertTrue(slack)
        self.assertTrue(sched)
        self.assertTrue(app.config["JOBHUNT_PRODUCTION"])

    def test_the_role_is_matched_loosely(self):
        """A stray newline out of an env file must not silently disable the Pi."""
        for value in ("Production", " production ", "PRODUCTION\n"):
            _, slack, sched = self.start(value)
            self.assertTrue(slack, repr(value))
            self.assertTrue(sched, repr(value))

    def test_an_unrecognised_role_starts_neither(self):
        _, slack, sched = self.start("staging")
        self.assertFalse(slack)
        self.assertFalse(sched)

    def test_the_web_ui_works_without_the_role(self):
        """Web-only is a useful mode, not a crippled one."""
        app, _, _ = self.start()
        app.config["TESTING"] = True
        self.assertEqual(app.test_client().get("/").status_code, 200)

    def test_the_test_overrides_still_win_in_production(self):
        """Test runs set these; they must not start a real Slack connection."""
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"
        _, slack, sched = self.start("production")
        self.assertFalse(slack)
        self.assertFalse(sched)


if __name__ == "__main__":
    unittest.main()
