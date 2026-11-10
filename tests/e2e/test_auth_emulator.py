"""Opt-in Firebase Auth integration; local demo accounts only, never outbound email.

Run ORCHESTRATOR_E2E_AUTH_EMULATOR=1 python3 -m unittest tests.e2e.test_auth_emulator -v
Requires node, npm and firebase CLI. Downloads the frontend's pinned Firebase SDK into
an automatically removed temporary directory. No project Firebase config is changed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.environ.get("ORCHESTRATOR_E2E_AUTH_EMULATOR") == "1", "opt-in local Firebase Auth emulator")
class AuthEmulatorTests(unittest.TestCase):
    def test_real_reset_request_code_confirmation_and_new_password_sign_in(self):
        for tool in ("node", "npm", "firebase"):
            self.assertIsNotNone(shutil.which(tool), f"{tool} is required")
        with tempfile.TemporaryDirectory(prefix="orchestrator-auth-emulator-") as directory:
            temp = Path(directory)
            # Match the deployed compat SDK rather than silently testing a newer release.
            html = (ROOT / "orchestrator/web/static/index.html").read_text()
            version = re.search(r"firebasejs/([\d.]+)/firebase-auth-compat.js", html).group(1)
            install = subprocess.run(
                ["npm", "install", "--prefix", directory, "--no-audit", "--no-fund", "--ignore-scripts", f"firebase@{version}"],
                capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = temp / "firebase.json"
            config.write_text(json.dumps({"emulators": {"auth": {"host": "127.0.0.1", "port": port}, "ui": {"enabled": False}, "singleProjectMode": True}}))
            helper = temp / "auth-emulator.cjs"
            helper.write_text((ROOT / "tests/e2e/auth_emulator.cjs").read_text())
            env = {**os.environ, "ORCHESTRATOR_RESET_MODULE": str(ROOT / "orchestrator/web/static/reset-password.js"), "CI": "true"}
            result = subprocess.run(
                ["firebase", "emulators:exec", "--only", "auth", "--project", "demo-orchestrator-auth-test", "--config", str(config), "node auth-emulator.cjs"],
                cwd=temp, env=env, capture_output=True, text=True, timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("PASS: reset requested, code checked and consumed, new password accepted, old password and reused code rejected", result.stdout)
