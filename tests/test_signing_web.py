"""The signing endpoints and actions: what the delivery page is told, who may ask, and what a copy needs before it starts."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import signing_keys
from orchestrator.web import server as ui
from tests.test_web_ui import ServerTestCase, UI_HEADERS

# stand-in key text, assembled here so no key-shaped block sits in the source
PEM = b"-----BEGIN " b"PRIVATE KEY-----\n" b"MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg" b"\n-----END " b"PRIVATE KEY-----\n"
KEY_ID = "MUD2T6SH8G"
NAME = f"AuthKey_{KEY_ID}.p8"
ISSUER = "6b2330c7-0203-4e58-9924-ba5c1f42e1a8"


class SigningWebTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name).resolve() / "home"
        (self.home / "Downloads").mkdir(parents=True)
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, **settings):
        path = self.root / ".orchestrator" / "project.json"
        config = json.loads(path.read_text()) if path.exists() else {}
        config.update(settings)
        path.write_text(json.dumps(config))

    def sign_in(self, email="tester@example.com", source="git"):
        with patch("orchestrator.web.server.verify_firebase_id_token", return_value={"email": email}), \
             patch("orchestrator.web.server.allowed_auth_sources", return_value={email: source}):
            res, data = self.request("POST", "/api/auth", body={"id_token": "id"}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 200, data)
        return data["token"]

    def as_member(self, token, method, path, body=None):
        headers = {"Authorization": f"Bearer {token}", **(UI_HEADERS if body is not None else {})}
        with patch("orchestrator.web.server.allowed_auth_sources", return_value={"tester@example.com": "settings"}):
            self.server.forget_allowed_emails()
            return self.request(method, path, body=body, headers=headers, auth=False)

    def test_the_view_tells_where_the_key_stands_without_looking_in_downloads(self):
        """A background service reading ~/Downloads makes macOS ask for permission and then waits, so the page-load call must not."""
        (self.home / "Downloads" / NAME).write_bytes(PEM)
        self.configure(delivery_platform="ios")
        with patch.object(signing_keys, "downloaded_keys", side_effect=AssertionError("must not look in Downloads on page load")), \
                patch.object(signing_keys, "issuer_hints", side_effect=AssertionError("must not read other projects on page load")):
            res, data = self.request("GET", "/api/signing")
        self.assertEqual(res.status, 200, data)
        self.assertEqual(data["ios"]["state"], "none")
        self.assertNotIn("downloads", data["ios"])
        self.assertNotIn("issuer_hints", data["ios"])
        self.assertIsNone(data["android"])
        self.assertEqual(data["machines"], [])
        self.assertNotIn("MIGTAgEAMBMG", json.dumps(data))

    def test_the_setup_dialog_asks_for_what_is_waiting_in_downloads_and_the_issuer_ids_other_projects_use(self):
        (self.home / "Downloads" / NAME).write_bytes(PEM)
        self.configure(delivery_platform="ios")
        res, data = self.request("GET", "/api/signing/setup")
        self.assertEqual(res.status, 200, data)
        self.assertEqual([d["name"] for d in data["downloads"]], [NAME])
        self.assertEqual(data["issuer_hints"], [])
        self.assertNotIn("MIGTAgEAMBMG", json.dumps(data))

    def test_only_an_owner_can_ask_what_is_in_downloads(self):
        (self.home / "Downloads" / NAME).write_bytes(PEM)
        token = self.sign_in(source="settings")
        res, data = self.as_member(token, "GET", "/api/signing/setup")
        self.assertEqual(res.status, 403, data)

    def test_it_follows_the_projects_delivery_type(self):
        self.configure(delivery_platform="web")
        res, data = self.request("GET", "/api/signing")
        self.assertEqual((data["ios"], data["android"]), (None, None))

    def test_a_ready_key_is_ready(self):
        self.configure(delivery_platform="ios", asc_key_id=KEY_ID, asc_issuer_id=ISSUER, asc_key_path=f"~/.private_keys/{NAME}")
        key = self.home / ".private_keys" / NAME
        key.parent.mkdir()
        key.write_bytes(PEM)
        key.chmod(0o600)
        self.assertEqual(self.request("GET", "/api/signing")[1]["ios"]["state"], "ready")

    def test_a_member_sees_the_state(self):
        self.configure(delivery_platform="ios")
        token = self.sign_in(source="settings")
        res, data = self.as_member(token, "GET", "/api/signing")
        self.assertEqual(res.status, 200, data)
        self.assertEqual(data["ios"]["state"], "none")

    def test_only_an_owner_can_search_other_computers(self):
        token = self.sign_in(source="settings")
        res, data = self.as_member(token, "POST", "/api/signing/find", {"platform": "ios"})
        self.assertEqual(res.status, 403, data)

    def test_the_search_runs_as_a_task_and_returns_which_computers_have_it(self):
        self.configure(delivery_platform="ios", asc_key_id=KEY_ID)
        fake = [{"name": "The Air", "has": True}, {"name": "Old Mac", "has": None}]
        with patch.object(signing_keys, "find_ios_elsewhere", return_value=fake) as find:
            res, data = self.request("POST", "/api/signing/find", body={"platform": "ios"}, headers=UI_HEADERS)
            self.assertEqual(res.status, 202, data)
            with self.server.tasks.condition:
                self.server.tasks.condition.wait_for(lambda: self.server.tasks._items[data["task"]]["status"] != "running", timeout=5)
            task = self.server.tasks.get(data["task"])
        self.assertEqual(task["result"], {"platform": "ios", "computers": fake})
        find.assert_called_once()

    def test_the_search_needs_a_key_id_to_look_for(self):
        self.configure(delivery_platform="ios")
        res, data = self.request("POST", "/api/signing/find", body={"platform": "ios"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 202)
        with self.server.tasks.condition:
            self.server.tasks.condition.wait_for(lambda: self.server.tasks._items[data["task"]]["status"] != "running", timeout=5)
        task = self.server.tasks.get(data["task"])
        self.assertEqual(task["status"], "error")
        self.assertIn("no App Store Connect key id", task["error"])


class SigningActionTests(unittest.TestCase):
    def build(self, action, **params):
        return ui.ACTIONS[action].build(params, Path(tempfile.gettempdir()))

    def test_the_actions_are_owner_only(self):
        for action in ("signing_import", "signing_secure", "signing_copy"):
            self.assertIn(action, ui.OWNER_ONLY_ACTIONS)

    def test_import_builds_the_command_for_the_named_download(self):
        argv = self.build("signing_import", file=NAME, issuer=ISSUER)
        self.assertEqual(argv[argv.index("signing"):], ["signing", "import-ios", "--file", NAME, "--issuer", ISSUER])
        self.assertIn("--replace", self.build("signing_import", file=NAME, issuer=ISSUER, replace=True))

    def test_import_refuses_anything_that_is_not_an_apple_key_name(self):
        for name in ("../AuthKey_X.p8", "key.p8", "AuthKey_ABC123DEFG.p8; rm -rf /", "/etc/passwd"):
            with self.subTest(name=name), self.assertRaises(ui.UIError):
                self.build("signing_import", file=name, issuer=ISSUER)

    def test_a_copy_cannot_be_started_without_the_acknowledgement(self):
        for ack in (None, False, "no", ""):
            with self.subTest(ack=ack), self.assertRaisesRegex(ui.UIError, "Confirm that you understand"):
                self.build("signing_copy", platform="ios", **{"from": "The Air", "to": "local"}, acknowledged=ack)

    def test_an_acknowledged_copy_builds_a_command_that_says_so(self):
        argv = self.build("signing_copy", platform="android", **{"from": "The Air", "to": "local"}, item="abcdef0123", acknowledged=True, replace=True)
        self.assertEqual(argv[argv.index("signing"):], ["signing", "copy", "--platform", "android", "--from", "The Air", "--to", "local",
                                                        "--i-understand", "--file", "abcdef0123", "--replace"])
        self.assertIn("--i-understand", argv)

    def test_machine_names_and_file_ids_are_validated(self):
        for bad in ({"from": "x; reboot", "to": "local"}, {"from": "The Air", "to": "$(id)"}, {"from": "The Air", "to": "local", "platform": "windows"},
                    {"from": "The Air", "to": "local", "item": "../../etc"}):
            with self.subTest(bad=bad), self.assertRaises(ui.UIError):
                self.build("signing_copy", **{"platform": "ios", "acknowledged": True, **bad})


if __name__ == "__main__":
    unittest.main()
