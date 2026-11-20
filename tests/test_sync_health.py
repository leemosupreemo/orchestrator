"""Does a computer say when it can't keep the others up to date? Blips are quiet; a failure past the grace period is
shown with what to do, and a success clears it."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import sync_health

ROOT = Path(__file__).resolve().parents[1]


class SyncHealthTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": tempfile.mkdtemp()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_blip_is_quiet_a_lasting_failure_is_shown_and_a_success_clears_it(self):
        sync_health.record("account", True, now=1000)
        sync_health.record("account", False, "<urlopen error [Errno 8] nodename nor servname provided>", now=2000)
        self.assertEqual(sync_health.problems(now=2000 + 60), [])  # a blip
        sync_health.record("account", False, "<urlopen error timed out>", now=2300)
        [problem] = sync_health.problems(now=2000 + sync_health.GRACE_SECONDS)
        self.assertEqual((problem["since"], problem["last_ok"], problem["label"]), (2000, 1000, "Checking in with your account"))
        self.assertIn("offline", problem["advice"])
        sync_health.record("account", True, now=2700)
        self.assertEqual(sync_health.problems(now=5000), [])

    def test_advice_names_the_usual_causes_and_never_a_token(self):
        self.assertIn("signed in", sync_health.advice("410 This computer was removed from its account"))
        self.assertIn("GitHub refused", sync_health.advice("remote: Permission to x/y.git denied to bot. 403"))
        self.assertIn("newer commits", sync_health.advice("! [rejected] ai/x -> ai/x (non-fast-forward)"))
        sync_health.record("branches", False, "fatal: unable to access 'https://user:ghp_secret@github.com/x/y.git/'", now=1)
        self.assertNotIn("ghp_secret", json.dumps(sync_health.overview(now=10_000)))

    def test_unknown_channels_are_ignored(self):
        sync_health.record("nope", False, "x", now=1)
        self.assertEqual(sync_health.overview(now=10_000)["problems"], [])


@unittest.skipUnless(shutil.which("node"), "node is required")
class BannerTests(unittest.TestCase):
    def test_home_and_the_job_page_say_what_failed_and_what_to_do(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        grab = lambda name, end: app[app.index(f"function {name}("):app.index(end, app.index(f"function {name}("))]
        script = f"""
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
const ago = () => "5m ago";
{grab("syncHealthBanner", "\n  const ")}
{grab("backupNote", "function strandedNote(")}
const health = {{ problems: [{{ label: "Checking in with your account", since: 1700000000, last_ok: 1699990000, error: "timed out", advice: "This computer seems to be offline." }}],
  channels: [{{ label: "Checking in with your account", failing_since: 1700000000, error: "timed out" }}, {{ label: "Sending job updates to your account", last_ok: 1 }}, {{ label: "Backing up unfinished work to GitHub" }}] }};
console.log(JSON.stringify({{
  banner: syncHealthBanner(health), none: syncHealthBanner({{ problems: [] }}),
  job: backupNote({{ status: "human-needed", backup_error: "line one\\nremote: Permission denied", wip_backup: {{ error: "x" }} }}),
  moved: backupNote({{ status: "human-needed", moved_to: "Air", backup_error: "x" }}),
}}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn("Checking in with your account hasn't worked since", out["banner"])
        self.assertIn("Your other computers see this one's jobs as of", out["banner"])
        self.assertIn("This computer seems to be offline.", out["banner"])
        self.assertIn("working, last 5m ago", out["banner"])
        self.assertIn("not used yet", out["banner"])
        self.assertEqual(out["none"], "")
        self.assertIn("Not backed up to GitHub: its latest commits and its unfinished files", out["job"])
        self.assertIn("remote: Permission denied", out["job"])
        self.assertEqual(out["moved"], "")


if __name__ == "__main__":
    unittest.main()
