"""Signing secrets per platform: where they are, importing a downloaded Apple key, and copying between computers.

Everything runs against temporary home folders with stand-in files. The SSH side uses a fake `ssh` that runs the very
command it is given in a shell, with HOME pointing at a separate "remote" folder, so the real quoting and shell logic are
exercised without any network or any real key."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import signing_keys as sk
from orchestrator.signing_keys import Machine, SecretError

# stand-in key text, assembled here so no key-shaped block sits in the source
PEM = b"-----BEGIN " b"PRIVATE KEY-----\n" b"MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg" b"\n-----END " b"PRIVATE KEY-----\n"
OTHER_PEM = b"-----BEGIN " b"PRIVATE KEY-----\n" b"DIFFERENTDIFFERENTDIFFERENT" b"\n-----END " b"PRIVATE KEY-----\n"
KEY_ID = "MUD2T6SH8G"
ISSUER = "6b2330c7-0203-4e58-9924-ba5c1f42e1a8"
NAME = f"AuthKey_{KEY_ID}.p8"


class TempWorld(unittest.TestCase):
    """A home folder, a project, and a second computer's home and project."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home, self.remote_home = base / "home", base / "remote-home"
        self.root, self.remote_root = base / "project", base / "remote-project"
        for folder in (self.home, self.remote_home, self.root, self.remote_root):
            folder.mkdir()
        patcher = patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.remote = Machine("The Air", "air.local", str(self.remote_root))
        self.local = Machine("local")
        self.messages: list[str] = []

    def write_config(self, **config):
        runtime = self.root / ".orchestrator"
        runtime.mkdir(exist_ok=True)
        (runtime / "project.json").write_text(json.dumps({"project_name": "Demo", "base_branch": "main", **config}))

    def key_in(self, home: Path, folder=".private_keys", data=PEM, mode=0o600, name=NAME) -> Path:
        path = home / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def fake_run(self, argv, data, timeout, *, tamper=None):
        """`ssh <options> <target> <command>`: run the command in a shell with the remote computer's HOME."""
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        command = argv[-1]
        done = subprocess.run(["sh", "-c", command], input=data, capture_output=True, env={**os.environ, "HOME": str(self.remote_home)})
        out = done.stdout
        if tamper and out:
            out = tamper(out)
        return done.returncode, out, done.stderr

    def copy(self, secret, source, destination, **kw):
        return sk.copy_secret(secret, source, destination, self.root, run=kw.pop("run", self.fake_run), say=self.messages.append, **kw)


class IosViewTests(TempWorld):
    def test_a_project_with_no_key_settings_is_none(self):
        self.assertEqual(sk.ios_view({"delivery_platform": "ios"}, self.root)["state"], "none")

    def test_other_delivery_types_have_no_ios_key(self):
        for platform in ("android", "file", "web"):
            self.assertIsNone(sk.ios_view({"delivery_platform": platform}, self.root))

    def test_a_key_id_alone_is_partial(self):
        self.assertEqual(sk.ios_view({"asc_key_id": KEY_ID}, self.root)["state"], "partial")

    def test_everything_set_and_the_file_here_is_ready(self):
        self.key_in(self.home)
        view = sk.ios_view({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"~/.private_keys/{NAME}"}, self.root)
        self.assertEqual((view["state"], view["file"], view["key_id"]), ("ready", NAME, KEY_ID))

    def test_the_owners_absolute_path_is_still_ready_when_the_file_is_in_this_computers_folder(self):
        self.key_in(self.home)
        view = sk.ios_view({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"/Users/someone/.private_keys/{NAME}"}, self.root)
        self.assertEqual(view["state"], "ready")

    def test_an_open_key_file_is_too_open(self):
        self.key_in(self.home, mode=0o644)
        view = sk.ios_view({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"~/.private_keys/{NAME}"}, self.root)
        self.assertEqual(view["state"], "too-open")

    def test_a_file_that_is_not_here_is_missing_and_says_whether_the_path_is_another_computers(self):
        view = sk.ios_view({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"/Users/ownername/.private_keys/{NAME}"}, self.root)
        self.assertEqual((view["state"], view["on_another_computer"]), ("missing", True))
        view = sk.ios_view({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"~/.private_keys/{NAME}"}, self.root)
        self.assertEqual((view["state"], view["on_another_computer"]), ("missing", False))

    def test_the_view_never_contains_key_contents(self):
        self.key_in(self.home)
        view = sk.status({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"~/.private_keys/{NAME}"}, self.root)
        self.assertNotIn("MIGTAgEAMBMG", json.dumps(view))


class DownloadsAndHintsTests(TempWorld):
    def test_downloaded_keys_are_found_by_name_and_content_newest_first(self):
        older = self.key_in(self.home, "Downloads", name="AuthKey_AAAAAA1111.p8")
        os.utime(older, (1000, 1000))
        self.key_in(self.home, "Desktop", name="AuthKey_BBBBBB2222.p8")
        self.key_in(self.home, "Downloads", data=b"not a key", name="AuthKey_CCCCCC3333.p8")
        self.key_in(self.home, "Downloads", name="notes.txt")
        (self.home / "Downloads" / "AuthKey_DDDDDD4444.p8").symlink_to(older)
        found = sk.downloaded_keys()
        self.assertEqual([k["id"] for k in found], ["BBBBBB2222", "AAAAAA1111"])
        self.assertEqual({k["folder"] for k in found}, {"Desktop", "Downloads"})
        self.assertNotIn("MIGTAgEAMBMG", json.dumps(found))

    def test_issuer_hints_come_from_other_projects_only(self):
        other = Path(self.temp.name) / "other"
        (other / ".orchestrator").mkdir(parents=True)
        (other / ".orchestrator" / "project.json").write_text(json.dumps({"asc_issuer_id": ISSUER}))
        self.write_config(asc_issuer_id="11111111-2222-3333-4444-555555555555")
        hints = sk.issuer_hints([("Other", other), ("Demo", self.root)], self.root)
        self.assertEqual(hints, [{"from": "Other", "issuer": ISSUER}])


class ImportTests(TempWorld):
    def setUp(self):
        super().setUp()
        self.write_config(delivery_platform="ios", development_team="ABC123DEFG", delivery_method="development")
        self.download = self.key_in(self.home, "Downloads", mode=0o644)

    def test_the_key_is_copied_into_the_secure_folder_and_the_project_is_told(self):
        result = sk.import_ios_key(self.root, NAME, ISSUER, say=self.messages.append)
        placed = self.home / ".private_keys" / NAME
        self.assertEqual(placed.read_bytes(), PEM)
        self.assertEqual(placed.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / ".private_keys").stat().st_mode & 0o777, 0o700)
        config = json.loads((self.root / ".orchestrator" / "project.json").read_text())
        self.assertEqual((config["asc_key_id"], config["asc_issuer_id"], config["asc_key_path"]), (KEY_ID, ISSUER, f"~/.private_keys/{NAME}"))
        self.assertEqual(config["project_name"], "Demo")  # the rest of the settings are kept
        self.assertTrue(self.download.exists())  # copied, never moved
        self.assertTrue(result["original_left"])
        self.assertEqual(result["original"], f"~/Downloads/{NAME}")
        self.assertEqual(sk.ios_view(config, self.root)["state"], "ready")
        self.assertNotIn("MIGTAgEAMBMG", " ".join(self.messages) + json.dumps(result))

    def test_the_issuer_and_the_file_name_are_checked(self):
        for name in ("../AuthKey_X.p8", "key.p8", "AuthKey_ABC.p8", f"AuthKey_{KEY_ID}.p8/../x"):
            with self.subTest(name=name), self.assertRaises(SecretError):
                sk.import_ios_key(self.root, name, ISSUER)
        for issuer in ("", "not-a-uuid", ISSUER[:-1]):
            with self.subTest(issuer=issuer), self.assertRaises(SecretError):
                sk.import_ios_key(self.root, NAME, issuer)

    def test_a_file_that_is_not_an_apple_key_is_refused_and_nothing_changes(self):
        self.download.write_bytes(b"hello")
        with self.assertRaisesRegex(SecretError, "doesn't look like an Apple API key"):
            sk.import_ios_key(self.root, NAME, ISSUER)
        self.assertFalse((self.home / ".private_keys").exists())
        self.assertNotIn("asc_key_id", json.loads((self.root / ".orchestrator" / "project.json").read_text()))

    def test_a_file_that_was_never_downloaded_says_where_to_get_it(self):
        self.download.unlink()
        with self.assertRaisesRegex(SecretError, "Apple only offers it once"):
            sk.import_ios_key(self.root, NAME, ISSUER)

    def test_a_different_key_already_in_place_is_not_replaced(self):
        placed = self.key_in(self.home, data=OTHER_PEM)
        with self.assertRaisesRegex(SecretError, "different file is already there"):
            sk.import_ios_key(self.root, NAME, ISSUER)
        self.assertEqual(placed.read_bytes(), OTHER_PEM)
        sk.import_ios_key(self.root, NAME, ISSUER, replace=True)
        self.assertEqual(placed.read_bytes(), PEM)

    def test_importing_twice_is_harmless(self):
        sk.import_ios_key(self.root, NAME, ISSUER)
        sk.import_ios_key(self.root, NAME, ISSUER)
        self.assertEqual((self.home / ".private_keys" / NAME).read_bytes(), PEM)

    def test_a_link_in_downloads_is_refused(self):
        self.download.unlink()
        real = self.key_in(self.home, "Documents", name="real.p8")
        (self.home / "Downloads" / NAME).symlink_to(real)
        with self.assertRaises(SecretError):
            sk.import_ios_key(self.root, NAME, ISSUER)

    def test_securing_makes_the_projects_key_owner_only(self):
        sk.import_ios_key(self.root, NAME, ISSUER)
        placed = self.home / ".private_keys" / NAME
        placed.chmod(0o644)
        sk.secure_ios_key(self.root)
        self.assertEqual(placed.stat().st_mode & 0o777, 0o600)


class AndroidViewTests(TempWorld):
    FLUTTER = """
def keystoreProperties = new Properties()
def keystorePropertiesFile = rootProject.file('key.properties')
if (keystorePropertiesFile.exists()) { keystoreProperties.load(new FileInputStream(keystorePropertiesFile)) }
plugins { id "com.android.application" }
android {
    signingConfigs {
        release {
            keyAlias keystoreProperties['keyAlias']
            storeFile keystoreProperties['storeFile'] ? file(keystoreProperties['storeFile']) : null
            storePassword keystoreProperties['storePassword']
        }
    }
    buildTypes { release { signingConfig signingConfigs.release } }
}
"""

    def app(self, gradle, folder="android/app", name="build.gradle"):
        module = self.root / folder
        module.mkdir(parents=True, exist_ok=True)
        (module / name).write_text(gradle)
        (module.parent / "settings.gradle").write_text("include ':app'\n")  # android/ is Gradle's root project, as in a Flutter app
        return module

    def view(self, **config):
        return sk.android_view({"delivery_platform": "android", **config}, self.root)

    def test_only_android_style_deliveries_apply(self):
        self.assertIsNone(sk.android_view({"delivery_platform": "ios"}, self.root))
        self.assertIsNone(sk.android_view({"delivery_platform": "file", "delivery_artifact": "out/*.ipa"}, self.root))
        self.assertEqual(sk.android_view({"delivery_platform": "file", "delivery_artifact": "out/*.apk"}, self.root)["state"], "no-project")

    def test_no_android_module_is_no_project(self):
        self.assertEqual(self.view()["state"], "no-project")

    def test_debug_signing_needs_no_key(self):
        self.app('plugins { id "com.android.application" }\nandroid { buildTypes { release { signingConfig signingConfigs.debug } } }')
        self.assertEqual(self.view()["state"], "not-needed")

    def test_release_signing_through_a_properties_file_that_is_not_here_is_missing(self):
        self.app(self.FLUTTER)
        view = self.view()
        self.assertEqual(view["state"], "missing")
        self.assertEqual([(f["kind"], f["here"], f["where"], f["rel"]) for f in view["files"]], [("settings", False, "project", "android/key.properties")])

    def test_properties_here_but_keystore_not_here_is_missing_the_keystore(self):
        self.app(self.FLUTTER)
        (self.root / "android" / "key.properties").write_text("storePassword=SECRETPW\nkeyPassword=SECRETPW\nkeyAlias=up\nstoreFile=upload-keystore.jks\n")
        view = self.view()
        self.assertEqual(view["state"], "missing")
        by_kind = {f["kind"]: f for f in view["files"]}
        self.assertTrue(by_kind["settings"]["here"])
        self.assertFalse(by_kind["keystore"]["here"])
        self.assertNotIn("SECRETPW", json.dumps(view))  # passwords in the file are never read out

    def test_everything_present_is_ready(self):
        module = self.app(self.FLUTTER)
        (self.root / "android" / "key.properties").write_text("storePassword=x\nstoreFile=upload-keystore.jks\n")
        (module / "upload-keystore.jks").write_bytes(b"\xfe\xed\xfe\xed" + b"0" * 40)
        view = self.view()
        self.assertEqual(view["state"], "ready")
        self.assertTrue(all(f["here"] for f in view["files"]))

    def test_a_keystore_path_in_another_users_home_is_flagged_as_another_computer(self):
        self.app(self.FLUTTER)
        (self.root / "android" / "key.properties").write_text("storeFile=/Users/ownername/keys/upload.jks\n")
        view = self.view()
        self.assertEqual((view["state"], view["on_another_computer"]), ("missing", True))
        keystore = next(f for f in view["files"] if f["kind"] == "keystore")
        self.assertFalse(keystore["copyable"])  # its path is in someone else's home: not copyable by name
        self.assertNotIn("id", keystore)

    def test_a_literal_keystore_in_the_module_is_found(self):
        module = self.app('plugins { id("com.android.application") }\nandroid { signingConfigs { create("release") { storeFile = file("../up.jks")\n storePassword = "x" } } }',
                          name="build.gradle.kts")
        self.assertEqual(self.view()["state"], "missing")
        (module.parent / "up.jks").write_bytes(b"\xfe\xed\xfe\xed" + b"0" * 40)
        self.assertEqual(self.view()["state"], "ready")

    def test_signing_from_environment_variables_is_reported_not_guessed(self):
        self.app('plugins { id "com.android.application" }\nandroid { signingConfigs { release { storeFile file(System.getenv("KEYSTORE_FILE")) } } }')
        view = self.view()
        self.assertEqual((view["state"], view["env"]), ("env", ["KEYSTORE_FILE"]))

    def test_unreadable_signing_setup_is_unknown(self):
        self.app('plugins { id "com.android.application" }\nandroid { signingConfigs { release { initWith(debug) } } }')
        self.assertEqual(self.view()["state"], "unknown")

    def test_each_file_has_a_stable_id_the_browser_can_name_it_by(self):
        self.app(self.FLUTTER)
        first, second = self.view()["files"][0]["id"], self.view()["files"][0]["id"]
        self.assertEqual(first, second)


class CopyTests(TempWorld):
    def test_pulling_the_key_from_another_computer(self):
        self.key_in(self.remote_home, data=PEM)
        secret = sk.ios_secret(KEY_ID)
        result = self.copy(secret, self.remote, self.local)
        placed = self.home / ".private_keys" / NAME
        self.assertEqual(placed.read_bytes(), PEM)
        self.assertEqual(placed.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["to"], "this computer")
        self.assertIn("identical", " ".join(self.messages))
        self.assertNotIn("MIGTAgEAMBMG", " ".join(self.messages))

    def test_pushing_the_key_to_another_computer(self):
        self.key_in(self.home)
        self.copy(sk.ios_secret(KEY_ID), self.local, self.remote)
        placed = self.remote_home / ".private_keys" / NAME
        self.assertEqual(placed.read_bytes(), PEM)
        self.assertEqual(placed.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.remote_home / ".private_keys").stat().st_mode & 0o777, 0o700)

    def test_the_other_secure_folder_is_found_on_the_other_computer(self):
        self.key_in(self.remote_home, ".appstoreconnect/private_keys")
        found = sk.ios_secret_on(self.remote, KEY_ID, self.root, self.fake_run)
        self.assertEqual(found.rel, f".appstoreconnect/private_keys/{NAME}")
        self.assertIsNone(sk.ios_secret_on(Machine("Elsewhere", "x.local", str(self.remote_root)), "OTHERKEY12", self.root, self.fake_run))

    def test_a_different_file_at_the_destination_is_never_replaced_unless_asked(self):
        self.key_in(self.remote_home)
        placed = self.key_in(self.home, data=OTHER_PEM)
        with self.assertRaisesRegex(SecretError, "different file is already"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)
        self.assertEqual(placed.read_bytes(), OTHER_PEM)
        self.copy(sk.ios_secret(KEY_ID), self.remote, self.local, replace=True)
        self.assertEqual(placed.read_bytes(), PEM)

    def test_an_identical_file_at_the_destination_is_fine(self):
        self.key_in(self.remote_home)
        self.key_in(self.home)
        self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)

    def test_a_remote_destination_that_differs_is_not_replaced_unless_asked(self):
        self.key_in(self.home)
        placed = self.key_in(self.remote_home, data=OTHER_PEM)
        with self.assertRaisesRegex(SecretError, "different file is already on The Air"):
            self.copy(sk.ios_secret(KEY_ID), self.local, self.remote)
        self.assertEqual(placed.read_bytes(), OTHER_PEM)

    def test_a_file_that_is_not_an_apple_key_is_not_copied(self):
        self.key_in(self.remote_home, data=b"just some text")
        with self.assertRaisesRegex(SecretError, "doesn't look like an Apple API key"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)
        self.assertFalse((self.home / ".private_keys").exists())

    def test_a_link_at_the_destination_is_never_written_through(self):
        self.key_in(self.remote_home)
        target = self.home / "elsewhere.txt"
        target.write_text("untouched")
        (self.home / ".private_keys").mkdir()
        (self.home / ".private_keys" / NAME).symlink_to(target)
        with self.assertRaises(SecretError):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local, replace=True)
        self.assertEqual(target.read_text(), "untouched")

    def test_a_link_at_a_remote_destination_is_never_written_through(self):
        self.key_in(self.home)
        target = self.remote_home / "elsewhere.txt"
        target.write_text("untouched")
        (self.remote_home / ".private_keys").mkdir()
        (self.remote_home / ".private_keys" / NAME).symlink_to(target)
        with self.assertRaises(SecretError):
            self.copy(sk.ios_secret(KEY_ID), self.local, self.remote, replace=True)
        self.assertEqual(target.read_text(), "untouched")

    def test_a_source_that_is_a_link_is_refused(self):
        real = self.remote_home / "real.p8"
        real.write_bytes(PEM)
        (self.remote_home / ".private_keys").mkdir()
        (self.remote_home / ".private_keys" / NAME).symlink_to(real)
        with self.assertRaisesRegex(SecretError, "isn't on The Air"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)

    def test_a_missing_source_says_so(self):
        with self.assertRaisesRegex(SecretError, "isn't on The Air"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)

    def test_an_unreachable_computer_gives_a_plain_message(self):
        def unreachable(argv, data, timeout):
            return 255, b"", b"ssh: connect to host air.local port 22: Connection refused"
        with self.assertRaisesRegex(SecretError, "Couldn't reach The Air over SSH"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local, run=unreachable)

    def test_a_copy_that_was_cut_short_is_caught(self):
        self.key_in(self.remote_home)
        run = lambda argv, data, timeout: self.fake_run(argv, data, timeout, tamper=lambda out: out[:-10] if out.startswith(b"-----BEGIN") else out)
        with self.assertRaises(SecretError):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local, run=run)
        self.assertFalse((self.home / ".private_keys" / NAME).exists())

    def test_a_copy_goes_between_this_computer_and_one_other(self):
        for a, b in ((self.local, self.local), (self.remote, self.remote)):
            with self.assertRaisesRegex(SecretError, "between this computer and one other"):
                self.copy(sk.ios_secret(KEY_ID), a, b)

    def test_locations_that_escape_are_refused(self):
        for rel in ("../outside.p8", "/etc/passwd", "a/../../b", "", "./x"):
            with self.subTest(rel=rel), self.assertRaises(SecretError):
                sk.Secret("android-keystore", "Keystore", "home", rel, 1000).parts()
        with self.assertRaises(SecretError):
            sk.Secret("android-keystore", "Keystore", "elsewhere", "a", 1000).parts()
        with self.assertRaises(SecretError):
            sk.ios_secret("../../etc")
        with self.assertRaises(SecretError):
            sk.ios_secret(KEY_ID, "Documents")

    def test_a_folder_that_is_a_link_out_of_the_home_folder_is_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.home / ".private_keys").symlink_to(outside)
        self.key_in(self.remote_home)
        with self.assertRaisesRegex(SecretError, "isn't allowed"):
            self.copy(sk.ios_secret(KEY_ID), self.remote, self.local)
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_target_that_looks_like_an_option_is_not_a_machine(self):
        runtime = self.root / ".orchestrator" / "config"
        runtime.mkdir(parents=True)
        (runtime / "machines.json").write_text(json.dumps({"machines": [
            {"name": "Bad", "execution_mode": "ssh", "ssh_target": "-oProxyCommand=evil", "repo_path": "/x"},
            {"name": "Good", "execution_mode": "ssh", "ssh_target": ["me@air.local"], "repo_path": "/x"},
            {"name": "NoPath", "execution_mode": "ssh", "ssh_target": "a.local"},
            {"name": "Here", "execution_mode": "local"}]}))
        self.assertEqual([(m.name, m.target) for m in sk.machines_from_config(self.root)], [("Good", "me@air.local")])

    def test_finding_which_computers_have_it(self):
        self.key_in(self.remote_home)
        secret = sk.ios_secret(KEY_ID)
        down = Machine("Down", "down.local", "/x")
        run = lambda argv, data, timeout: (255, b"", b"") if "down.local" in argv else self.fake_run(argv, data, timeout)
        self.assertEqual(sk.find_elsewhere([secret], [self.remote, down], self.root, run), [{"name": "The Air", "has": True}, {"name": "Down", "has": None}])


class ProjectFileCopyTests(TempWorld):
    """A secret inside a project folder (Android's key.properties) must only ever land in a git-ignored place."""

    def setUp(self):
        super().setUp()
        for folder in (self.root, self.remote_root):
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
        self.entry = {"kind": "settings", "label": "Signing settings (key.properties)", "where": "project", "rel": "key.properties"}
        (self.remote_root / "key.properties").write_text("storePassword=SECRETPW\nstoreFile=up.jks\n")

    def test_it_copies_when_the_file_is_git_ignored_on_this_computer(self):
        (self.root / ".gitignore").write_text("key.properties\n")
        self.copy(sk.android_secret(self.entry), self.remote, self.local)
        self.assertEqual((self.root / "key.properties").read_text(), "storePassword=SECRETPW\nstoreFile=up.jks\n")
        self.assertEqual((self.root / "key.properties").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("SECRETPW", " ".join(self.messages))

    def test_it_refuses_when_the_file_would_be_committed_here(self):
        with self.assertRaisesRegex(SecretError, "isn't git-ignored in this project"):
            self.copy(sk.android_secret(self.entry), self.remote, self.local)
        self.assertFalse((self.root / "key.properties").exists())

    def test_it_refuses_to_push_into_a_place_that_is_not_ignored_over_there(self):
        (self.root / "key.properties").write_text("storePassword=x\n")
        with self.assertRaisesRegex(SecretError, "isn't git-ignored on The Air"):
            self.copy(sk.android_secret({**self.entry}), self.local, self.remote, replace=True)

    def test_it_pushes_when_ignored_there(self):
        (self.remote_root / ".gitignore").write_text("key.properties\n")
        (self.remote_root / "key.properties").unlink()
        (self.root / "key.properties").write_text("storePassword=x\n")
        self.copy(sk.android_secret(self.entry), self.local, self.remote)
        self.assertEqual((self.remote_root / "key.properties").read_text(), "storePassword=x\n")
        self.assertEqual((self.remote_root / "key.properties").stat().st_mode & 0o777, 0o600)

    def test_files_outside_the_project_and_home_cannot_be_copied_by_name(self):
        with self.assertRaisesRegex(SecretError, "can't be copied by name"):
            sk.android_secret({"kind": "keystore", "label": "Release keystore", "where": "", "rel": ""})


if __name__ == "__main__":
    unittest.main()
