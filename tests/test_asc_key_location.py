"""The App Store Connect key lives on one computer, but a project's settings are shared.

Themis's committed settings name /Users/<owner>/.private_keys/AuthKey_<id>.p8. On another computer that path is someone
else's folder, so the key is found by its ID in this computer's own folders, and when it isn't there the error says so
instead of claiming the folder is the wrong kind."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import project_config
from tests.test_config_validation import make_config

OWNER_PATH = "/Users/ownername/.private_keys/AuthKey_MUD2T6SH8G.p8"


class AscKeyLocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home = base / "otheruser"          # this computer's user: not the one in the committed path
        self.root = base / "project"
        self.home.mkdir()
        self.root.mkdir()
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, key_path=OWNER_PATH, key_id="MUD2T6SH8G"):
        return make_config(self.root, development_team="ABC123DEFG", delivery_method="debugging",
                           asc_key_id=key_id, asc_issuer_id="ISSUER123", asc_key_path=key_path)

    def put_key(self, relative=".private_keys", mode=0o600, name="AuthKey_MUD2T6SH8G.p8"):
        folder = self.home / relative
        folder.mkdir(parents=True, exist_ok=True)
        key = folder / name
        key.write_text("fake-key-data")
        key.chmod(mode)
        return key

    def test_the_owners_path_is_replaced_by_the_key_found_by_id_on_this_computer(self):
        key = self.put_key()
        self.assertEqual(project_config.locate_asc_key(OWNER_PATH, "MUD2T6SH8G", self.root), key.resolve())
        self.assertEqual(self.config().validate_distribution_config(), [])

    def test_the_other_secure_folder_works_too(self):
        key = self.put_key(".appstoreconnect/private_keys")
        self.assertEqual(project_config.locate_asc_key(OWNER_PATH, "MUD2T6SH8G", self.root), key.resolve())
        self.assertEqual(self.config().validate_distribution_config(), [])

    def test_a_key_that_is_not_here_says_it_is_on_another_computer_and_what_to_do(self):
        errors = self.config().validate_distribution_config()
        self.assertEqual(len(errors), 1)
        message = errors[0]
        self.assertIn("isn't on this computer", message)
        self.assertIn(OWNER_PATH, message)
        self.assertIn("AuthKey_MUD2T6SH8G.p8", message)
        self.assertIn("~/.private_keys/", message)
        self.assertIn("never git", message)
        self.assertNotIn("not in a designated secure Xcode directory", message)

    def test_a_key_inside_the_repository_keeps_the_original_explanation(self):
        message = self.config(key_path=".secrets/AuthKey_KEY123.p8", key_id="KEY123").validate_distribution_config()[0]
        self.assertIn("not in a designated secure Xcode directory", message)
        self.assertNotIn("another computer", message)

    def test_an_absolute_path_on_this_computer_but_outside_the_secure_folders_keeps_the_original_explanation(self):
        stray = self.home / "Downloads" / "AuthKey_MUD2T6SH8G.p8"
        stray.parent.mkdir()
        stray.write_text("x")
        message = self.config(key_path=str(stray)).validate_distribution_config()[0]
        self.assertIn("not in a designated secure Xcode directory", message)

    def test_a_key_here_with_open_permissions_is_still_reported(self):
        self.put_key(mode=0o644)
        errors = self.config().validate_distribution_config()
        self.assertEqual(len(errors), 1)
        self.assertIn("insecure permissions", errors[0])

    def test_a_link_in_the_secure_folder_to_somewhere_else_is_not_trusted(self):
        elsewhere = self.home / "Documents" / "AuthKey_MUD2T6SH8G.p8"
        elsewhere.parent.mkdir()
        elsewhere.write_text("x")
        (self.home / ".private_keys").mkdir()
        (self.home / ".private_keys" / "AuthKey_MUD2T6SH8G.p8").symlink_to(elsewhere)
        self.assertTrue(self.config().validate_distribution_config())

    def test_a_key_id_that_is_not_plain_letters_and_digits_is_never_used_to_build_a_path(self):
        self.put_key()
        for hostile in ("../.private_keys/AuthKey_MUD2T6SH8G", "MUD2T6SH8G/../x", "", "A B", "M*"):
            with self.subTest(key_id=hostile):
                found = project_config.locate_asc_key(OWNER_PATH, hostile, self.root)
                self.assertEqual(found, Path(OWNER_PATH))  # the configured path, unchanged

    def test_a_key_on_this_computer_in_the_configured_place_is_used_as_is(self):
        key = self.put_key()
        self.assertEqual(project_config.locate_asc_key(str(key), "MUD2T6SH8G", self.root), key.resolve())

    def test_a_tilde_path_means_this_computers_home(self):
        key = self.put_key()
        self.assertEqual(project_config.locate_asc_key("~/.private_keys/AuthKey_MUD2T6SH8G.p8", "MUD2T6SH8G", self.root), key.resolve())

    def test_nothing_is_created_or_moved(self):
        before = sorted(p.name for p in self.home.rglob("*"))
        self.config().validate_distribution_config()
        project_config.locate_asc_key(OWNER_PATH, "MUD2T6SH8G", self.root)
        self.assertEqual(sorted(p.name for p in self.home.rglob("*")), before)


class SetupCheckTests(unittest.TestCase):
    """`orchestrator check-config`-style report uses the same answer as distribution."""

    def run_check(self, key_path, home):
        from orchestrator.scripts import check_setup
        mock_config = MagicMock()
        mock_config.firebase_distribution = True
        mock_config.project_name = "SampleApp"
        mock_config.development_team = "TEAM123"
        mock_config.asc_key_id = "MUD2T6SH8G"
        mock_config.asc_issuer_id = "ISSUER123"
        mock_config.asc_key_path = key_path
        mock_config.provisioning_profile_specifier = ""
        mock_config.xcode_project = None
        mock_config.xcode_workspace = None
        with patch.object(Path, "home", return_value=home), patch.object(check_setup, "PROJECT_CONFIG", mock_config), \
                patch("sys.stdout", new_callable=io.StringIO) as out, patch("check_setup.check_file_content", return_value=True), \
                patch("shutil.which", return_value=None):
            check_setup.check()
        return out.getvalue()

    def test_another_computers_key_path_is_explained_and_a_local_copy_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve() / "otheruser"
            home.mkdir()
            output = self.run_check(OWNER_PATH, home)
            self.assertIn("ASC API KEYS (NOT ON THIS COMPUTER)", output)
            self.assertIn("isn't on this computer", output)
            self.assertNotIn("INSECURE LOCATION", output)
            key = home / ".private_keys" / "AuthKey_MUD2T6SH8G.p8"
            key.parent.mkdir()
            key.write_text("fake")
            key.chmod(0o600)
            output = self.run_check(OWNER_PATH, home)
            self.assertIn("ASC API KEYS (HEADLESS)", output)
            self.assertIn("FOUND", output)


if __name__ == "__main__":
    unittest.main()
