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

    def test_clear_failures_drops_stale_errors_but_keeps_last_ok(self):
        sync_health.record("product", True, now=1000)
        sync_health.record("product", False, "network error", now=2000)
        self.assertEqual(len(sync_health.problems(now=2000 + sync_health.GRACE_SECONDS)), 1)
        sync_health.clear_failures()
        self.assertEqual(sync_health.problems(now=2000 + sync_health.GRACE_SECONDS), [])
        channels = {c["channel"]: c for c in sync_health.overview(now=3000)["channels"]}
        self.assertEqual(channels["product"]["last_ok"], 1000)
        self.assertIsNone(channels["product"]["failing_since"])

    def test_stale_pre_sleep_failure_is_not_reported_until_retried(self):
        sync_health.record("product", True, now=1000)
        sync_health.record("product", False, "network error", now=2000)
        # After an hour of sleep with no recent retry, don't flash a stale failure before a fresh check runs
        wake_now = 2000 + sync_health.STALE_FAILURE_SECONDS + 60
        self.assertEqual(sync_health.problems(now=wake_now), [])
        # A single transient failure right after wake starts a fresh grace window instead of reviving the pre-sleep timestamp
        sync_health.record("product", False, "network error", now=wake_now)
        self.assertEqual(sync_health.problems(now=wake_now + 5), [])
        # If it keeps failing past the grace window, it IS reported with the fresh start time
        sync_health.record("product", False, "network error", now=wake_now + sync_health.GRACE_SECONDS)
        [problem] = sync_health.problems(now=wake_now + sync_health.GRACE_SECONDS)
        self.assertEqual(problem["since"], wake_now)

    def test_shared_ui_server_forwards_wake_state_sync(self):
        from orchestrator.web.server import UIServer
        local = UIServer(("127.0.0.1", 0), None)
        remote = UIServer(("127.0.0.1", 0), None, shared=local)
        try:
            self.assertIs(remote._state_sync_wake, local._state_sync_wake)
            self.assertFalse(local._state_sync_wake.is_set())
            remote.wake_state_sync()
            self.assertTrue(local._state_sync_wake.is_set())
        finally:
            remote.server_close()
            local.server_close()

    def test_home_auto_rechecks_sync_health_and_server_clears_mid_round_sleep(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        server = (ROOT / "orchestrator" / "web" / "server.py").read_text(encoding="utf-8")
        self.assertIn("for (const delay of [5000, 15000])", app)
        self.assertIn('const fresh = await api("sync-health").catch(() => null);', app)
        self.assertIn("if time.time() - round_start > 300.0:", server)


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
const health = {{ problems: [{{ channel: "account", label: "Checking in with your account", since: 1700000000, last_ok: 1699990000, error: "timed out", advice: "This computer seems to be offline." }}],
  channels: [{{ channel: "account", label: "Checking in with your account", failing_since: 1700000000, error: "timed out" }}, {{ label: "Sending job updates to your account", last_ok: 1 }}, {{ label: "Backing up unfinished work to GitHub" }}] }};
const shared = {{ problems: [
  {{ channel: "product", label: "Sharing product requirements", since: 1732123800, last_ok: 1732123740, advice: "It keeps retrying." }},
  {{ channel: "shared_jobs", label: "Sharing jobs", since: 1732125600, last_ok: 1732125480, advice: "It keeps retrying." }},
  {{ channel: "branches", label: "Backing up job branches", since: 1732125600 }},
], channels: [{{ channel: "shared_jobs", label: "Sharing jobs", failing_since: 1732125600, error: "<Git error>" }}] }};
console.log(JSON.stringify({{
  banner: syncHealthBanner(health), none: syncHealthBanner({{ problems: [] }}),
  shared: syncHealthBanner(shared),
  job: backupNote({{ status: "human-needed", backup_error: "line one\\nremote: Permission denied", wip_backup: {{ error: "x" }} }}),
  moved: backupNote({{ status: "human-needed", moved_to: "Air", backup_error: "x" }}),
}}));
"""
        env = {**os.environ, "TZ": "UTC", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
        out = json.loads(subprocess.run(["node", "-e", script], env=env, capture_output=True, text=True, check=True).stdout)
        self.assertIn("<strong>Checking in with your account</strong> has been failing since", out["banner"])
        self.assertNotIn("Your other computers see this one's jobs", out["banner"])
        self.assertIn("This computer seems to be offline.", out["banner"])
        self.assertIn("working, last 5m ago", out["banner"])
        self.assertIn("not used yet", out["banner"])
        self.assertEqual(out["none"], "")
        self.assertIn("<strong>Sharing product requirements</strong> has been failing since Nov 20, 5:30 PM. Last successful check Nov 20, 5:29 PM.", out["shared"])
        self.assertIn("<strong>Sharing jobs</strong> has been failing since Nov 20, 6:00 PM. Last successful check Nov 20, 5:58 PM.", out["shared"])
        self.assertIn("Waiting for the first successful check.", out["shared"])
        self.assertEqual(out["shared"].count("It keeps retrying."), 1)
        self.assertIn("<strong>Sharing jobs</strong>: &lt;Git error>", out["shared"])
        self.assertIn("Not backed up to GitHub: its latest commits and its unfinished files", out["job"])
        self.assertIn("remote: Permission denied", out["job"])
        self.assertEqual(out["moved"], "")


if __name__ == "__main__":
    unittest.main()
