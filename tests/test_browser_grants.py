import http.client
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.web.browser_grants import BrowserGrantStore
from orchestrator.web.server import UIServer


class BrowserGrantTests(unittest.TestCase):
    def test_grant_expires_at_sixty_seconds(self):
        now = [100.0]
        store = BrowserGrantStore(clock=lambda: now[0])
        grant = store.issue("#/setup")
        now[0] = 160.0
        self.assertIsNone(store.consume(grant))

    def test_one_consumer_wins_concurrent_replay(self):
        store = BrowserGrantStore()
        grant = store.issue("#/projects")
        result = []
        threads = [threading.Thread(target=lambda: result.append(store.consume(grant))) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(result.count("#/projects"), 1)

    def test_external_routes_are_refused(self):
        store = BrowserGrantStore()
        for route in ("https://evil.example", "//evil.example", "#/\r\nevil", "#/file?path=config/settings.json"):
            with self.assertRaises(ValueError):
                store.issue(route)

    def test_tunnel_cannot_redeem_grant_and_local_redirect_is_clean(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": folder}):
            local = UIServer(("127.0.0.1", 0), None, token="secret", desktop_listener=True)
            remote = UIServer(("127.0.0.1", 0), None, shared=local)
            threads = []
            for server in (local, remote):
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
                thread.start()
                threads.append(thread)
            try:
                grant = local.browser_grants.issue("#/setup")
                conn = http.client.HTTPConnection("127.0.0.1", remote.server_address[1])
                conn.request("GET", f"/desktop/open?grant={grant}", headers={"X-Forwarded-For": "127.0.0.1"})
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 404)
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", local.server_address[1])
                conn.request("GET", f"/desktop/open?grant={grant}")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/#/setup")
                self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
                self.assertIn("SameSite=Strict", response.getheader("Set-Cookie"))
                self.assertNotIn(grant, str(response.getheaders()))
                conn.close()
                conn = http.client.HTTPConnection("127.0.0.1", local.server_address[1])
                conn.request("GET", f"/desktop/open?grant={grant}")
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 401)
                conn.close()
            finally:
                for server in (local, remote):
                    server.shutdown()
                    server.server_close()
                for thread in threads:
                    thread.join(1)

    def test_ui_token_uses_isolated_private_state(self):
        from orchestrator.web.server import get_or_create_ui_token
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": folder}):
            first = get_or_create_ui_token()
            self.assertEqual(get_or_create_ui_token(), first)
            path = Path(folder) / "ui_token"
            self.assertEqual(path.read_text(), first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
