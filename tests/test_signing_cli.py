"""`orchestrator signing ...` as a real command: flags, exit codes and what it prints (never a key).

HOME is a temp folder, and `ssh` on PATH is a script that runs the command it's given with HOME set to another temp folder
(the "other computer"), so the SSH paths run for real without a network or any real key."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# stand-in key text, assembled here so no key-shaped block sits in the source
PEM = b"-----BEGIN " b"PRIVATE KEY-----\n" b"MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBHkwdwIBAQQg" b"\n-----END " b"PRIVATE KEY-----\n"
KEY_ID = "MUD2T6SH8G"
NAME = f"AuthKey_{KEY_ID}.p8"
ISSUER = "6b2330c7-0203-4e58-9924-ba5c1f42e1a8"
REPO = Path(__file__).resolve().parents[1]


class SigningCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.home, self.remote_home, self.root, self.remote_root = (base / n for n in ("home", "remote-home", "project", "remote-project"))
        for folder in (self.home, self.remote_home, self.root, self.remote_root, base / "bin"):
            folder.mkdir()
        ssh = base / "bin" / "ssh"
        ssh.write_text('#!/bin/bash\ncmd="${@: -1}"\nHOME="$FAKE_REMOTE_HOME" exec sh -c "$cmd"\n')
        ssh.chmod(0o755)
        for folder in (self.root, self.remote_root):
            subprocess.run(["git", "init", "-q", str(folder)], check=True)
        self.env = {**os.environ, "HOME": str(self.home), "FAKE_REMOTE_HOME": str(self.remote_home), "PATH": f"{base / 'bin'}:{os.environ['PATH']}",
                    "ORCHESTRATOR_USER_STATE_DIR": str(base / "state"), "ORCHESTRATOR_NONINTERACTIVE": "1"}
        (self.root / ".orchestrator" / "config").mkdir(parents=True)
        self.write_config({})
        (self.root / ".orchestrator" / "config" / "machines.json").write_text(json.dumps({"machines": [
            {"name": "The Air", "execution_mode": "ssh", "ssh_target": "air.local", "repo_path": str(self.remote_root)}]}))

    def write_config(self, extra):
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo", "base_branch": "main", "delivery_platform": "ios", **extra}))

    def signing(self, *args):
        done = subprocess.run([sys.executable, "-m", "orchestrator", "signing", *args, "--project", str(self.root)], cwd=REPO, env=self.env,
                              capture_output=True, text=True, timeout=60)
        self.assertNotIn("MIGTAgEAMBMG", done.stdout + done.stderr)  # a key is never printed
        return done.returncode, done.stdout

    def put(self, path: Path, data=PEM, mode=0o600):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data) if isinstance(data, bytes) else path.write_text(data)
        path.chmod(mode)
        return path

    def test_status_says_where_the_ios_key_stands(self):
        code, out = self.signing("status")
        self.assertEqual(code, 0)
        self.assertIn("iOS signing key: none", out)
        self.write_config({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"/Users/ownername/.private_keys/{NAME}"})
        self.assertIn("iOS signing key: missing", self.signing("status")[1])
        self.put(self.home / ".private_keys" / NAME)
        self.assertIn("iOS signing key: ready", self.signing("status")[1])

    def test_status_for_a_project_with_no_signing_key_says_so(self):
        self.write_config({"delivery_platform": "web"})
        self.assertIn("no signing key to check", self.signing("status")[1])

    def test_importing_a_download(self):
        self.put(self.home / "Downloads" / NAME, mode=0o644)
        code, out = self.signing("import-ios", "--file", NAME, "--issuer", ISSUER)
        self.assertEqual(code, 0, out)
        self.assertIn(f"the key {KEY_ID} is ready", out)
        self.assertIn("The download is still at ~/Downloads", out)
        self.assertEqual((self.home / ".private_keys" / NAME).stat().st_mode & 0o777, 0o600)
        config = json.loads((self.root / ".orchestrator" / "project.json").read_text())
        self.assertEqual((config["asc_key_id"], config["asc_key_path"]), (KEY_ID, f"~/.private_keys/{NAME}"))

    def test_importing_needs_both_flags_and_a_good_issuer(self):
        self.assertEqual(self.signing("import-ios", "--file", NAME)[0], 2)
        code, out = self.signing("import-ios", "--file", NAME, "--issuer", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Issuer ID", out)

    def test_copy_does_nothing_without_the_acknowledgement(self):
        self.write_config({"asc_key_id": KEY_ID})
        self.put(self.remote_home / ".private_keys" / NAME)
        code, out = self.signing("copy", "--platform", "ios", "--from", "The Air", "--to", "local")
        self.assertEqual(code, 2)
        self.assertIn("--i-understand", out)
        self.assertFalse((self.home / ".private_keys").exists())

    def test_copying_the_ios_key_from_another_computer(self):
        self.write_config({"asc_key_id": KEY_ID, "asc_issuer_id": ISSUER, "asc_key_path": f"/Users/ownername/.private_keys/{NAME}"})
        self.put(self.remote_home / ".appstoreconnect" / "private_keys" / NAME)  # found in the other secure folder, lands in ~/.private_keys
        code, out = self.signing("copy", "--platform", "ios", "--from", "The Air", "--to", "local", "--i-understand")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.home / ".private_keys" / NAME).read_bytes(), PEM)
        self.assertIn("identical to the original", out)
        self.assertIn("iOS signing key: ready", self.signing("status")[1])

    def test_copying_a_key_the_other_computer_does_not_have(self):
        self.write_config({"asc_key_id": KEY_ID})
        code, out = self.signing("copy", "--platform", "ios", "--from", "The Air", "--to", "local", "--i-understand")
        self.assertEqual(code, 1)
        self.assertIn(f"AuthKey_{KEY_ID}.p8 isn't on The Air", out)

    def test_an_unknown_computer_is_refused_with_the_known_ones_listed(self):
        self.write_config({"asc_key_id": KEY_ID})
        code, out = self.signing("copy", "--platform", "ios", "--from", "Nowhere", "--to", "local", "--i-understand")
        self.assertEqual(code, 1)
        self.assertIn("Known: The Air", out)

    def test_pushing_the_ios_key_to_another_computer(self):
        self.write_config({"asc_key_id": KEY_ID})
        self.put(self.home / ".private_keys" / NAME)
        code, out = self.signing("copy", "--platform", "ios", "--from", "local", "--to", "The Air", "--i-understand")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.remote_home / ".private_keys" / NAME).read_bytes(), PEM)

    def test_copying_android_signing_files_in_the_order_they_become_known(self):
        gradle = ("plugins { id 'com.android.application' }\nandroid { signingConfigs { release {\n"
                  "storeFile keystoreProperties['storeFile'] ? file(keystoreProperties['storeFile']) : null\n} } }\n"
                  "def keystorePropertiesFile = rootProject.file('key.properties')\n")
        for root in (self.root, self.remote_root):
            self.put(root / "android" / "app" / "build.gradle", gradle, 0o644)
            self.put(root / "android" / "settings.gradle", "include ':app'\n", 0o644)
            self.put(root / ".gitignore", "key.properties\n*.jks\n", 0o644)
        self.write_config({"delivery_platform": "android"})
        self.put(self.remote_root / "android" / "key.properties", "storePassword=SECRETPW\nstoreFile=upload.jks\n")
        self.put(self.remote_root / "android" / "app" / "upload.jks", b"\xfe\xed\xfe\xed" + b"0" * 64)
        code, out = self.signing("copy", "--platform", "android", "--from", "The Air", "--to", "local", "--i-understand")
        self.assertEqual(code, 0, out)
        self.assertIn("2 files copied and checked", out)
        self.assertNotIn("SECRETPW", out)
        self.assertEqual((self.root / "android" / "key.properties").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "android" / "app" / "upload.jks").read_bytes(), b"\xfe\xed\xfe\xed" + b"0" * 64)
        self.assertIn("Android signing key: ready", self.signing("status")[1])
        code, out = self.signing("copy", "--platform", "android", "--from", "The Air", "--to", "local", "--i-understand")
        self.assertIn("nothing to copy", out)

    def test_android_files_that_would_be_committed_are_refused(self):
        gradle = "plugins { id 'com.android.application' }\nandroid { signingConfigs { release { storeFile file('up.jks') } } }\n"
        self.put(self.root / "android" / "app" / "build.gradle", gradle, 0o644)
        self.put(self.remote_root / "android" / "app" / "build.gradle", gradle, 0o644)
        self.put(self.remote_root / "android" / "app" / "up.jks", b"\xfe\xed\xfe\xed" + b"0" * 64)
        self.write_config({"delivery_platform": "android"})  # no .gitignore for the keystore here
        code, out = self.signing("copy", "--platform", "android", "--from", "The Air", "--to", "local", "--i-understand")
        self.assertEqual(code, 1)
        self.assertIn("isn't git-ignored", out)
        self.assertFalse((self.root / "android" / "app" / "up.jks").exists())


if __name__ == "__main__":
    unittest.main()
