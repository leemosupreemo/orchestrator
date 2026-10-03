import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import account, enroll
from orchestrator.project_setup import apply_project_setup

SECRET = "machine-secret-never-printed"
TOKEN = "enroll_" + "t" * 32


class Fake:
    """Everything enroll touches outside Python."""

    def __init__(self, base: Path, user="builder"):
        self.base, self.user = base, user
        self.calls, self.redeemed = [], []
        self.ready = True
        self.login_item = "enabled"
        self.app = base / "Applications/Orchestrator.app"

    def system(self, **overrides):
        values = dict(console_user=lambda: self.user, current_user=lambda: "builder", is_root=lambda: False,
                      run=self.run, redeem=self.redeem, agent_ready=lambda timeout: self.ready, app=self.app,
                      registration=lambda since, timeout: self.login_item,
                      projects_dir=self.base / "Projects")
        values.update(overrides)
        return enroll.System(**values)

    def run(self, argv, **kwargs):
        argv = list(map(str, argv))
        self.calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            target = Path(argv[-1])
            target.mkdir(parents=True)
            (target / "Package.swift").write_text("// swift-tools-version:5.9\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def redeem(self, body):
        self.redeemed.append(body)
        if body["token"] != TOKEN:
            raise account.AccountError("That enrollment command isn't valid or has expired.", 404)
        return {"machine_id": "m_0123456789abcdef", "machine_secret": SECRET, "owner_email": "Owner@Example.com"}


class EnrollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="orch-enroll-")
        self.base = Path(self.tmp.name).resolve()
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.base / "state"), "HOME": str(self.base)})
        self.env.start()
        self.fake = Fake(self.base)
        self.project = self.base / "Projects/app"
        self.project.mkdir(parents=True)
        apply_project_setup(self.project, {"build_command": "true", "test_command": "true", "models": ["codex"]})

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def enroll(self, project=None, token=TOKEN, system=None, **kwargs):
        out = io.StringIO()
        code = enroll.enroll(token, str(project or self.project), system=system or self.fake.system(), out=out.write, **kwargs)
        return code, out.getvalue()

    def test_enrolls_registers_the_app_and_never_prints_secrets(self):
        code, text = self.enroll()
        self.assertEqual(code, 0, text)
        machine = account.load_machine()
        self.assertEqual(machine["machine_secret"], SECRET)
        self.assertEqual(machine["owner_email"], "owner@example.com")
        self.assertIn(["/usr/bin/open", "-g", "-j", "-a", str(self.fake.app), "--args", "--register-background"], self.fake.calls)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(TOKEN, text)
        self.assertIn("owner@example.com", text)

    def test_root_and_missing_console_session_are_refused(self):
        code, text = self.enroll(system=self.fake.system(is_root=lambda: True))
        self.assertEqual(code, 1)
        self.assertIn("not as root", text)
        self.fake.user = "root"
        code, text = self.enroll()
        self.assertEqual(code, 1)
        self.assertIn("automatic login", text)
        self.assertEqual(self.fake.redeemed, [])
        self.assertIsNone(account.load_machine())

    def test_app_on_a_mounted_image_is_refused(self):
        code, text = self.enroll(system=self.fake.system(app=Path("/Volumes/Orchestrator/Orchestrator.app")))
        self.assertEqual(code, 1)
        self.assertIn("Applications", text)
        self.assertEqual(self.fake.redeemed, [])

    def test_already_connected_mac_keeps_its_identity_and_leaves_the_token_unused(self):
        account.save_machine({"machine_id": "m_ffffffffffffffff", "machine_secret": "old", "owner_email": "first@example.com"})
        code, text = self.enroll()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.fake.redeemed, [])
        self.assertEqual(account.load_machine()["machine_id"], "m_ffffffffffffffff")
        self.assertIn("first@example.com", text)

    def test_bad_project_fails_before_the_token_is_used(self):
        code, text = self.enroll(project=self.base / "missing")
        self.assertEqual(code, 1)
        self.assertEqual(self.fake.redeemed, [])
        self.assertIn("missing", text)

    def test_git_url_is_cloned_into_projects_without_prompting(self):
        code, text = self.enroll(project="git@github.com:me/closet-app.git")
        self.assertEqual(code, 0, text)
        clone = next(call for call in self.fake.calls if call[:2] == ["git", "clone"])
        self.assertEqual(clone[-1], str(self.base / "Projects/closet-app"))
        self.assertTrue((self.base / "Projects/closet-app/.orchestrator/project.json").is_file())

    def test_existing_folder_is_never_overwritten_by_a_clone(self):
        (self.base / "Projects/closet-app").mkdir()
        code, text = self.enroll(project="https://github.com/me/closet-app.git")
        self.assertEqual(code, 1)
        self.assertFalse(any(call[:2] == ["git", "clone"] for call in self.fake.calls))
        self.assertIn("already exists", text)

    def test_unknown_project_type_lists_what_is_missing_and_keeps_the_clone(self):
        def run(argv, **kwargs):
            argv = list(map(str, argv))
            self.fake.calls.append(argv)
            if argv[:2] == ["git", "clone"]:
                Path(argv[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        code, text = self.enroll(project="https://github.com/me/empty.git", system=self.fake.system(run=run))
        self.assertEqual(code, 1)
        self.assertTrue((self.base / "Projects/empty").is_dir())
        self.assertIn("build", text.lower())
        self.assertEqual(self.fake.redeemed, [])

    def test_expired_token_explains_and_saves_nothing(self):
        code, text = self.enroll(token="enroll_expired")
        self.assertEqual(code, 1)
        self.assertIn("expired", text)
        self.assertIsNone(account.load_machine())

    def test_agent_that_never_starts_points_to_login_item_approval(self):
        self.fake.ready = False
        code, text = self.enroll()
        self.assertEqual(code, 1)
        self.assertIn("Login Items", text)
        self.assertIsNotNone(account.load_machine())

    def test_login_item_needing_approval_is_reported_not_claimed(self):
        self.fake.login_item = "requiresApproval"
        code, text = self.enroll()
        self.assertEqual(code, 1)
        self.assertIn("won't start again after a restart", text)
        self.assertNotIn("starting at login", text)
        self.fake.login_item = ""
        self.assertEqual(self.enroll()[0], 1)

    def test_login_item_waiting_for_work_is_explained(self):
        self.fake.login_item = "pending"
        code, text = self.enroll()
        self.assertEqual(code, 0, text)
        self.assertIn("once its current work finishes", text)

    def test_outside_the_app_it_pairs_and_explains_the_service(self):
        code, text = self.enroll(system=self.fake.system(app=None))
        self.assertEqual(code, 0, text)
        self.assertFalse(any(call[0] == "/usr/bin/open" for call in self.fake.calls))
        self.assertIn("orchestrator service install", text)

    def test_token_from_stdin(self):
        with patch("sys.stdin", io.StringIO(TOKEN + "\n")):
            self.assertEqual(enroll.read_token(None, from_stdin=True), TOKEN)
        with self.assertRaises(ValueError):
            enroll.read_token(None, from_stdin=False)


class ContainingAppTests(unittest.TestCase):
    def test_finds_the_app_around_its_runtime(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder).resolve() / "Orchestrator.app"
            python = app / "Contents/Resources/runtime/bin/python3"
            python.parent.mkdir(parents=True)
            (app / "Contents/Info.plist").write_bytes(b"<plist/>")
            self.assertEqual(enroll.containing_app(python), app)
            self.assertIsNone(enroll.containing_app(Path(folder) / "venv/bin/python3"))


if __name__ == "__main__":
    unittest.main()
