"""A job waiting for a fix says what is wrong, in words, on its page and in the Run fix dialog."""
from __future__ import annotations

import glob
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from orchestrator import job_problem

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "orchestrator" / "web" / "static"
BLOCKED = "All five writes were blocked by the session's permission mode — no file on disk was changed."
ANALYSIS = "## Top blocker\n- **Zero tests discovered (`NO TESTS RAN`)**: The runner found nothing.\n\n## Other real failures\n- None\n"


class DescribeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        state = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.out / "user")})  # never this computer's real sign-ins
        state.start()
        self.addCleanup(state.stop)
        self.job = {"job_id": "j", "status": "debugging", "test_status": "tests-failed"}

    def test_only_a_job_in_debugging_has_a_problem(self):
        self.assertIsNone(job_problem.describe({"status": "completed"}, self.out))

    def test_the_real_vegetation_job_leads_with_the_expired_sign_in_then_the_rest(self):
        (self.out / "test.log").write_text("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n")
        (self.out / "builder_summary.md").write_text(BLOCKED)
        self.job.update(last_error="No model could answer (tried: claude-sonnet-4-6). Last error: Failed to authenticate. API Error: 401 OAuth access token has expired.",
                        debug_history=[{"hypothesis": "All five file writes were silently skipped due to a permissions block."}])
        out = job_problem.describe(self.job, self.out)
        self.assertEqual(out["summary"], "The last fix attempt couldn't run: your Claude sign-in expired. Sign in again, then run the fix.")
        self.assertEqual(out["sign_in"], {"provider": "claude", "name": "Claude"})
        text = " ".join(out["lines"])
        self.assertIn("edits were blocked", text)
        self.assertIn("No tests ran", text)
        self.assertIn("diagnosis: All five file writes", text)

    def test_signing_in_again_clears_the_expired_sign_in_but_keeps_the_rest(self):
        (self.out / "test.log").write_text("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n")
        self.job.update(last_error="Failed to authenticate (claude). API Error: 401 OAuth access token has expired.",
                        updated_at="2026-11-16T10:00:00")
        self.assertEqual(job_problem.describe(self.job, self.out)["sign_in"], {"provider": "claude", "name": "Claude"})
        job_problem.record_sign_in("codex", at=datetime(2026, 11, 16, 11).timestamp())  # another tool: still expired
        self.assertIsNotNone(job_problem.describe(self.job, self.out)["sign_in"])
        job_problem.record_sign_in("claude", at=datetime(2026, 11, 16, 9).timestamp())  # before the error: still expired
        self.assertIsNotNone(job_problem.describe(self.job, self.out)["sign_in"])
        job_problem.record_sign_in("claude", at=datetime(2026, 11, 16, 11).timestamp())
        out = job_problem.describe(self.job, self.out)
        self.assertIsNone(out["sign_in"])
        self.assertNotIn("sign-in", out["summary"])
        self.assertIn("No tests ran", out["summary"])
        self.assertNotIn("stopped with an error", " ".join(out["lines"]))  # the old sign-in error isn't repeated as "an error"

    def test_closing_the_sign_in_dialog_redraws_the_page(self):
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("onClose: () => { refreshState().then(() => route()).catch(() => {}); }", app)
        connect = (ROOT / "orchestrator" / "scripts" / "ai_connect.py").read_text(encoding="utf-8")
        self.assertIn("job_problem.record_sign_in(args.provider)", connect)

    def test_no_tests_ran_is_a_test_setup_problem(self):
        (self.out / "test.log").write_text("Ran 0 tests in 0.000s\n\nNO TESTS RAN\n")
        out = job_problem.describe(self.job, self.out)
        self.assertTrue(out["summary"].startswith("No tests ran"))
        self.assertIn("not a code failure", out["summary"])
        self.assertIsNone(out["sign_in"])

    def test_failing_tests_are_named(self):
        (self.out / "test.log").write_text("FAIL: test_slope (tests.T)\nAssertionError: 3 != 4\n\nFAILED (failures=1)\n")
        self.assertIn("test_slope", job_problem.describe(self.job, self.out)["summary"])

    def test_a_build_failure_uses_the_build_analysis(self):
        (self.out / "build_analysis_2026-11-15_19-40-32.md").write_text("## Top blocker\n- `Foo.cs(12)`: missing semicolon\n")
        out = job_problem.describe(dict(self.job, test_status="build-failed"), self.out)
        self.assertEqual(out["summary"], "The project doesn't build. Foo.cs(12): missing semicolon")

    def test_the_ai_analysis_is_used_when_the_log_names_nothing(self):
        (self.out / "test.log").write_text("something broke\n")
        (self.out / "test_analysis_2026-11-15_19-44-57.md").write_text(ANALYSIS)
        self.assertIn("Zero tests discovered (NO TESTS RAN)", job_problem.describe(self.job, self.out)["summary"])

    def test_another_error_and_a_pause_are_reported_plainly(self):
        self.job.update(last_error="Disk is full", debug_phase="paused", debug_pause_reason="Loop/Stall detected: same fix twice")
        out = job_problem.describe(self.job, self.out)
        self.assertIn("Disk is full", " ".join([out["summary"], *out["lines"]]))
        self.assertIn("Automatic fixing paused: Loop/Stall detected", " ".join(out["lines"]))

    def test_with_nothing_recorded_it_still_says_something(self):
        self.assertEqual(job_problem.describe({"status": "debugging"}, self.out / "missing")["summary"], "The build or tests didn't pass.")


class WiringTests(unittest.TestCase):
    def test_the_job_page_and_the_dialog_show_it(self):
        app = (STATIC / "app.js").read_text()
        self.assertIn('const rawHeroReason = String(problem?.summary || s.state.reason || "").trim();', app)
        self.assertIn("const signIn = problem?.sign_in || s.state.sign_in;", app)
        self.assertIn("Sign in to ${esc(signIn.name)}", app)
        self.assertIn("<strong>What needs fixing</strong>", app)
        self.assertIn("(await api(`jobs/${encodeURIComponent(params.job)}`)).problem", app)
        server = Path(glob.glob(str(ROOT / "orchestrator" / "web" / "ser*.py"))[0]).read_text()
        self.assertIn('"problem": job_problem_for(job, out_dir)', server)


if __name__ == "__main__":
    unittest.main()
