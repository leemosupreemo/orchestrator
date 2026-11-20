"""Setting up a project says whether its requirements and jobs will be shared with the person's other computers, and asks for
public repositories: on by default for private ones, off unless chosen for public ones (docs/state-sync.md)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import new_project as np, state_ref  # noqa: E402
from tests.test_new_project import ANSWERS, Isolated  # noqa: E402
from tests.test_web_ui import UI_HEADERS, ServerTestCase  # noqa: E402

APP = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()


class PolicyTests(unittest.TestCase):
    def test_public_repositories_ask_first_and_private_ones_share_by_default(self):
        self.assertTrue(state_ref.SHARE_BY_DEFAULT)
        self.assertFalse(state_ref.SHARE_PUBLIC_BY_DEFAULT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin", "https://github.com/alice/shop.git"], cwd=root, check=True)
            with patch.object(state_ref, "is_public", return_value=True):
                self.assertFalse(state_ref.sharing(root))
                self.assertEqual(state_ref.status(root)["state"], "ask")
            with patch.object(state_ref, "is_public", return_value=False):
                self.assertTrue(state_ref.sharing(root))
                self.assertEqual(state_ref.status(root)["state"], "on")
            with patch.object(state_ref, "is_public", return_value=None):  # a host that can't say: treated as private
                self.assertTrue(state_ref.sharing(root))
            state_ref.set_consent(root, True)
            with patch.object(state_ref, "is_public", return_value=True):
                self.assertTrue(state_ref.sharing(root))  # an explicit yes on a public repository is a yes


class NewProjectTests(Isolated):
    def test_the_wizard_keeps_the_answer_and_records_it_for_both_kinds(self):
        self.assertIsNone(np.save_draft({"answers": ANSWERS})["share"])
        self.assertTrue(np.save_draft({"answers": ANSWERS, "share": True})["share"])
        self.assertTrue(np.save_draft({"answers": ANSWERS, "share": "yes"})["share"])  # not a bool: keeps what was saved
        root = self.home / "proj"
        root.mkdir()
        step = np.record_sharing(root, False, "private")
        self.assertEqual([state_ref.consent(root, s) for s in state_ref.SCOPES], [False, False])
        self.assertIn("Off", step["detail"])

    def test_with_no_answer_private_shares_and_public_does_not(self):
        private, public = self.home / "a", self.home / "b"
        private.mkdir()
        public.mkdir()
        np.record_sharing(private, None, "private")
        np.record_sharing(public, None, "public")
        self.assertEqual((state_ref.consent(private), state_ref.consent(private, "jobs")), (True, True))
        self.assertEqual((state_ref.consent(public), state_ref.consent(public, "jobs")), (False, False))

    def test_creating_on_github_records_the_answer_and_says_so(self):
        draft = {"answers": ANSWERS, "host": "github", "visibility": "public", "share": True, "parent": str(self.home / "Projects")}
        with patch.object(np, "publish_to_github", return_value={"name": "Create the GitHub repository", "ok": True, "detail": "x"}):
            result = np.create_project(draft)
        root = Path(result["root"])
        self.assertEqual(state_ref.consent(root, "jobs"), True)
        self.assertIn("Share with your other computers", [s["name"] for s in result["steps"]])

    def test_a_local_project_records_nothing_so_the_default_applies_once_it_has_a_remote(self):
        result = np.create_project({"answers": ANSWERS, "host": "local", "share": False, "parent": str(self.home / "Projects")})
        self.assertIsNone(state_ref.consent(Path(result["root"])))


class ImportTests(ServerTestCase):
    def folder(self, remote="https://gitlab.example.com/team/shop.git"):
        path = Path(self.tmp.name) / "existing"
        path.mkdir(exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, capture_output=True)
        return path

    def test_setup_shows_whether_it_will_share_and_records_the_answer(self):
        path = self.folder()
        res, info = self.request("GET", f"/api/projects/import-info?path={path}")
        self.assertEqual((res.status, info["sharing"]["state"], info["sharing"]["remote"]), (200, "on", "gitlab.example.com/team/shop"))
        res, _ = self.request("POST", "/api/projects/import", body={"root": str(path), "share": False}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual([state_ref.consent(path, s) for s in state_ref.SCOPES], [False, False])

    def test_without_an_answer_nothing_is_recorded(self):
        path = self.folder()
        self.request("POST", "/api/projects/import", body={"root": str(path)}, headers=UI_HEADERS)
        self.assertIsNone(state_ref.consent(path))


class PageTests(unittest.TestCase):
    def test_both_setup_screens_carry_the_choice(self):
        helper = APP[APP.index("function setupSharingHtml("):APP.index("// ---------------------------------------------------------------- import project page")]
        self.assertIn("No Git remote yet, so its requirements and jobs stay on this computer", helper)
        self.assertIn("This repository is public, so anyone could read them. Leave this off", helper)
        self.assertIn('${setupSharingHtml(info?.sharing, { id: "ip-share" })}', APP)
        self.assertIn('share: $("#ip-share").checked', APP)
        self.assertIn('<input type="checkbox" name="share"', APP)               # the new-project wizard
        self.assertIn('form.share.checked = form.visibility.value !== "public"', APP)  # its default follows the visibility


if __name__ == "__main__":
    unittest.main()
