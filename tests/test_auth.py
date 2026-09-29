"""Web authentication.

The app refuses to bind to a LAN address without JOBHUNT_PASSWORD set. That
guard is only meaningful if the password is actually checked — an earlier
version had the refusal without the check, which is worse than no password
because it implies protection that is not there.
"""

import base64
import os
import tempfile
import unittest
from pathlib import Path

from jobhunt import app as app_module
from jobhunt import db as db_module


def basic(password, user="jobhunt"):
    token = base64.b64encode("{0}:{1}".format(user, password).encode()).decode()
    return {"Authorization": "Basic " + token}


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = db_module.DB_PATH
        db_module.DB_PATH = Path(self._tmp.name) / "t.db"
        self._orig_env = dict(os.environ)
        # Keep the Slack listener and scheduler out of the test process.
        os.environ["JOBHUNT_NO_SLACK"] = "1"
        os.environ["JOBHUNT_NO_SCHEDULER"] = "1"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._orig_env)
        db_module.DB_PATH = self._orig_db
        self._tmp.cleanup()

    def client(self, password=None):
        if password:
            os.environ["JOBHUNT_PASSWORD"] = password
        else:
            os.environ.pop("JOBHUNT_PASSWORD", None)
        application = app_module.create_app()
        application.config["TESTING"] = True
        return application.test_client()


class TestPasswordEnforcement(AuthTestCase):
    def test_no_password_configured_means_open_access(self):
        """Loopback-only development needs no login."""
        self.assertEqual(self.client().get("/").status_code, 200)

    def test_request_without_credentials_is_rejected(self):
        response = self.client("s3cret").get("/")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Basic", response.headers.get("WWW-Authenticate", ""))

    def test_wrong_password_is_rejected(self):
        response = self.client("s3cret").get("/", headers=basic("wrong"))
        self.assertEqual(response.status_code, 401)

    def test_correct_password_is_accepted(self):
        response = self.client("s3cret").get("/", headers=basic("s3cret"))
        self.assertEqual(response.status_code, 200)

    def test_every_route_is_protected_not_just_the_index(self):
        client = self.client("s3cret")
        for path in ("/", "/config", "/sources", "/job/1", "/document/1"):
            self.assertEqual(client.get(path).status_code, 401, path)

    def test_document_download_is_protected(self):
        """Generated CVs are personal data and must not be world-readable."""
        response = self.client("s3cret").get("/document/1")
        self.assertEqual(response.status_code, 401)

    def test_post_routes_are_protected(self):
        client = self.client("s3cret")
        for path in ("/fetch", "/run", "/config/save"):
            self.assertEqual(client.post(path).status_code, 401, path)


class TestBindGuard(unittest.TestCase):
    """A non-loopback bind without a password must not be possible."""

    def test_the_guard_message_names_the_variable(self):
        import inspect

        source = inspect.getsource(app_module.main)
        self.assertIn("JOBHUNT_PASSWORD", source)
        self.assertIn("SystemExit", source)


class TestCrossSiteWrites(AuthTestCase):
    """CSRF defence: browsers auto-attach Basic credentials, so a malicious
    website could otherwise fire authenticated POSTs at the app."""

    def test_cross_site_post_is_rejected(self):
        client = self.client()
        response = client.post("/fetch", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_cross_site_referer_is_rejected_when_origin_missing(self):
        client = self.client()
        response = client.post(
            "/fetch", headers={"Referer": "https://evil.example/attack.html"})
        self.assertEqual(response.status_code, 403)

    def test_same_origin_post_is_accepted(self):
        client = self.client()
        response = client.post(
            "/job/1/status", data={"status": "dismissed"},
            headers={"Origin": "http://localhost"})
        # 404 (no such job) proves it passed the origin gate.
        self.assertEqual(response.status_code, 404)

    def test_headerless_post_is_allowed_for_curl_and_scripts(self):
        """CSRF needs a browser; browsers always send Origin cross-site."""
        client = self.client()
        response = client.post("/job/1/status", data={"status": "dismissed"})
        self.assertEqual(response.status_code, 404)

    def test_gets_are_never_blocked(self):
        client = self.client()
        response = client.get("/", headers={"Referer": "https://evil.example"})
        self.assertEqual(response.status_code, 200)

    def test_cross_site_is_rejected_even_with_auth_enabled(self):
        """The origin gate and the auth gate must both hold; either 401 or
        403 is a correct refusal for a credential-less cross-site POST."""
        client = self.client("s3cret")
        response = client.post("/fetch", headers={"Origin": "https://evil.example"})
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
