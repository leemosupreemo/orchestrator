"""Signing in to Sentry with its device flow: a code to approve in the browser, no token to create or paste."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import sentry_device as D  # noqa: E402


class FakeSentry:
    """Answers the three calls the way sentry.io does (docs.sentry.io/api/guides/oauth/, Device Authorization Flow)."""

    def __init__(self, answers: list[tuple[int, dict]] | None = None, orgs: list[dict] | None = None):
        self.answers = list(answers or [])
        self.orgs = orgs if orgs is not None else [{"slug": "acme", "name": "Acme"}]
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, str]] = []

    def post(self, url: str, form: dict) -> tuple[int, dict]:
        self.posts.append((url, form))
        if url.endswith("/oauth/device/code/"):
            return 200, {"device_code": "dev-123", "user_code": "ABCD-EFGH", "verification_uri": "https://sentry.io/oauth/device/",
                         "verification_uri_complete": "https://sentry.io/oauth/device/?user_code=ABCD-EFGH", "expires_in": 600, "interval": 5}
        return self.answers.pop(0)

    def get(self, url: str, token: str) -> list[dict]:
        self.gets.append((url, token))
        return self.orgs


class DeviceFlowTests(unittest.TestCase):
    def test_starting_asks_for_read_access_and_returns_a_code_to_approve(self):
        fake = FakeSentry()
        started = D.start("client-1", post=fake.post)
        url, form = fake.posts[0]
        self.assertEqual(url, "https://sentry.io/oauth/device/code/")
        self.assertEqual(form, {"client_id": "client-1", "scope": "org:read project:read event:read event:write"})
        self.assertEqual((started["user_code"], started["url"], started["interval"], started["expires_in"]),
                         ("ABCD-EFGH", "https://sentry.io/oauth/device/?user_code=ABCD-EFGH", 5, 600))
        self.assertEqual(started["device_code"], "dev-123")

    def test_polling_waits_slows_down_and_finishes_with_the_token(self):
        fake = FakeSentry([(400, {"error": "authorization_pending"}), (400, {"error": "slow_down"}),
                           (200, {"access_token": "tok", "refresh_token": "ref", "expires_at": "2026-12-09T10:00:00Z", "scope": "org:read"})])
        self.assertEqual(D.poll("client-1", "dev-123", post=fake.post), {"status": "pending"})
        self.assertEqual(D.poll("client-1", "dev-123", post=fake.post), {"status": "pending", "slow_down": True})
        done = D.poll("client-1", "dev-123", post=fake.post)
        self.assertEqual((done["status"], done["token"], done["expires"]), ("done", "tok", "2026-12-09"))
        url, form = fake.posts[-1]
        self.assertEqual(url, "https://sentry.io/oauth/token/")
        self.assertEqual(form, {"client_id": "client-1", "device_code": "dev-123", "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        self.assertNotIn("client_secret", form)  # a public app has none

    def test_a_refusal_or_an_expired_code_ends_it_with_plain_words(self):
        denied = D.poll("c", "d", post=FakeSentry([(400, {"error": "access_denied"})]).post)
        self.assertEqual(denied["status"], "denied")
        self.assertIn("wasn't approved", denied["message"])
        expired = D.poll("c", "d", post=FakeSentry([(400, {"error": "expired_token"})]).post)
        self.assertEqual(expired["status"], "expired")
        self.assertIn("Start again", expired["message"])
        other = D.poll("c", "d", post=FakeSentry([(400, {"error": "invalid_client"})]).post)
        self.assertEqual(other["status"], "error")

    def test_the_organization_comes_from_the_token(self):
        fake = FakeSentry(orgs=[{"slug": "acme", "name": "Acme"}])
        self.assertEqual(D.organization("tok", get=fake.get), {"slug": "acme", "name": "Acme"})
        self.assertEqual(fake.gets, [("https://sentry.io/api/0/organizations/", "tok")])
        with self.assertRaises(D.DeviceError):
            D.organization("tok", get=FakeSentry(orgs=[]).get)

    def test_the_client_id_comes_from_the_environment_settings_or_the_built_in_one(self):
        self.assertEqual(D.client_id({"sentry_client_id": " abc "}, env={}), "abc")
        self.assertEqual(D.client_id({"sentry_client_id": "abc"}, env={"ORCHESTRATOR_SENTRY_CLIENT_ID": "env"}), "env")
        self.assertEqual(D.client_id({}, env={}), D.CLIENT_ID)


class SavedConnectionTests(unittest.TestCase):
    def test_a_signed_in_connection_keeps_its_end_date_for_the_reminder(self):
        from unittest.mock import patch
        from orchestrator import integrations
        with patch.object(integrations.PROVIDERS["sentry"], "test", lambda self: "Acme"):
            creds, who = integrations.connect("sentry", {"host": "sentry.io", "org": "acme", "token": "tok", "expires": "2026-12-09", "auth_type": "oauth"})
        self.assertEqual((creds["org"], creds["token"], creds["auth_type"], creds["expires"], who), ("acme", "tok", "oauth", "2026-12-09", "Acme"))


if __name__ == "__main__":
    unittest.main()
