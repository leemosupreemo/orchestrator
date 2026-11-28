"""A job waiting for a fix says what is wrong, in words, on its page and in the Run fix dialog."""
from __future__ import annotations

import glob
import json
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
        self.assertIn("if (!retried) refreshState().then(() => route()).catch(() => {});", app)
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

    def test_failed_job_also_describes_its_problem(self):
        out = job_problem.describe({"status": "failed", "last_error": "Builder timed out after 600s"}, self.out)
        self.assertIsNotNone(out)
        self.assertIn("Builder timed out after 600s", out["summary"])

    def test_paused_and_failed_job_state_surface_real_error_and_skip_cleared_disk_blocker(self):
        from orchestrator.web import server as web_server
        with patch.object(web_server, "worker_alive", return_value=False):
            paused_err = web_server.job_state(
                {"status": "executing", "last_error": "Codex exited with status 1", "plan": {"tasks": ["A", "B"]}},
            )
            self.assertEqual(paused_err["label"], "Paused")
            self.assertEqual(paused_err["tone"], "failed")
            self.assertIn("Codex exited with status 1", paused_err["reason"])

            paused_no_err = web_server.job_state(
                {"status": "executing", "plan": {"tasks": ["A", "B"]}},
            )
            self.assertEqual(paused_no_err["label"], "Paused")
            self.assertIn("worker stopped unexpectedly", paused_no_err["reason"])

            with patch("orchestrator.blockers.is_cleared", return_value=True):
                paused_cleared = web_server.job_state(
                    {"status": "executing", "last_error": "Not enough disk space on this computer. Free at least 2 GB.", "plan": {"tasks": ["A", "B"]}},
                    root=self.out,
                )
                self.assertNotIn("disk space", paused_cleared["reason"].lower())
                self.assertIn("worker stopped unexpectedly", paused_cleared["reason"])

        failed = web_server.job_state({"status": "failed", "last_error": "Missing Unity license"})
        self.assertEqual(failed["label"], "Failed")
        self.assertIn("Missing Unity license", failed["reason"])

    def test_debugging_states_are_specific_and_long_diagnosis_is_not_cut_off(self):
        from orchestrator.web import server as web_server
        long_hyp = (
            "Task 1 is already implemented in the working tree and passing all locally runnable Python checks, "
            "while Task 2 still needs the Swift client endpoint wiring, retry handling, and offline unit tests "
            "before the branch is ready to merge."
        )
        # 1. Passing fix attempt in debugging status is ready to merge/review, not "Needs a fix"
        passed_job = {
            "job_id": "j-pass",
            "status": "debugging",
            "pr_number": 42,
            "debug_history": [{"iteration": 1, "hypothesis": long_hyp, "result": {"build_ok": True, "tests_ok": True}}],
        }
        self.assertTrue(job_problem.last_fix_passed(passed_job))
        self.assertIsNone(job_problem.describe(passed_job, self.out))
        st_pass = web_server.job_state(passed_job)
        self.assertEqual((st_pass["label"], st_pass["next"]["action"]), ("Ready to merge", "merge"))

        # 2. Paused auto-fix leads with pause reason and keeps the full diagnosis without cutting at 240 chars
        paused_job = {
            "job_id": "j-pause",
            "status": "debugging",
            "debug_phase": "paused",
            "debug_pause_reason": "Loop/Stall detected: repeated hypothesis",
            "debug_history": [{"iteration": 2, "hypothesis": long_hyp, "result": {"build_ok": True, "tests_ok": False}}],
        }
        st_pause = web_server.job_state(paused_job)
        self.assertEqual((st_pause["label"], st_pause["tone"], st_pause["next"]["action"]), ("Auto-fix paused", "failed", "debug"))
        prob_pause = job_problem.describe(paused_job, self.out)
        self.assertTrue(prob_pause["summary"].startswith("Automatic fixing paused:"))
        self.assertIn(long_hyp, " ".join(prob_pause["lines"]))

        # 3. Interrupted/pending fix attempt says it stopped before finishing and keeps the full diagnosis
        pending_job = {
            "job_id": "j-pend",
            "status": "debugging",
            "debug_history": [{"iteration": 1, "hypothesis": long_hyp, "result": "pending"}],
        }
        st_pend = web_server.job_state(pending_job)
        self.assertEqual((st_pend["label"], st_pend["tone"], st_pend["next"]["action"]), ("Fix stopped", "attention", "debug"))
        prob_pend = job_problem.describe(pending_job, self.out)
        self.assertEqual(prob_pend["summary"], "The last fix attempt stopped before finishing.")
        self.assertIn(long_hyp, " ".join(prob_pend["lines"]))

        # 4. Build/test failure recorded in debug_history (without test_status) still surfaces clear labels
        build_fail = web_server.job_state({
            "status": "debugging",
            "debug_history": [{"iteration": 1, "hypothesis": long_hyp, "result": {"build_ok": False, "tests_ok": False}}],
        })
        self.assertEqual((build_fail["label"], build_fail["tone"]), ("Build failing", "failed"))
        test_fail = web_server.job_state({
            "status": "debugging",
            "debug_history": [{"iteration": 1, "hypothesis": long_hyp, "result": {"build_ok": True, "tests_ok": False}}],
        })
        self.assertEqual((test_fail["label"], test_fail["tone"]), ("Tests failing", "failed"))

        # 5. Hypothesis-only debugging job explains what happened and never truncates at 240 chars
        hyp_only = job_problem.describe({"status": "debugging", "debug_history": [{"hypothesis": long_hyp}]}, self.out)
        self.assertIn("didn't finish", hyp_only["summary"])
        self.assertIn(long_hyp, hyp_only["summary"])


class WiringTests(unittest.TestCase):
    def test_the_job_page_shows_the_problem_and_fix_runs_directly(self):
        app = (STATIC / "app.js").read_text()
        self.assertIn('const rawHeroReason = String(problem?.summary || s.state.reason || "").trim();', app)
        self.assertIn("const signIn = problem?.sign_in || s.state.sign_in;", app)
        self.assertIn("Sign in to ${esc(signIn.name)}", app)
        self.assertNotIn('formDialog("Run fix"', app)
        self.assertNotIn("async debug(params)", app)
        self.assertIn('if (action === "debug" && params.logs === undefined && state.project?.remote_logs)', app)
        self.assertIn('runAction(action, params, { button: el });', app)
        server = Path(glob.glob(str(ROOT / "orchestrator" / "web" / "ser*.py"))[0]).read_text()
        self.assertIn('"problem": job_problem_for(job, out_dir)', server)

    def test_hero_reason_clamps_long_problem_with_show_more_and_debug_steer_has_hint(self):
        import subprocess
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('hint: "Nothing is running. Leave blank to let the AI try again, or add notes to guide the next fix."', app)
        self.assertIn('problem && !problem.sign_in ? (typeof clamped === "function" ? clamped(effectiveHeroReason, 240) : esc(effectiveHeroReason))', app)
        # Verify in Node that clamped + conciseReasonText never cut mid-word and always keep full text accessible.
        clamp_start = app.index("function clamped(")
        clamp_src = app[clamp_start:app.index("document.addEventListener", clamp_start)]
        concise_start = app.index("const conciseReasonText = ")
        concise_src = app[concise_start:app.index("const architectConcerns =", concise_start)]
        script = f"""
const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
{clamp_src}
{concise_src}
const longText = "The last fix attempt didn't finish. Its diagnosis: Task 1 is already implemented in the working tree and passing all locally runnable Python checks, while Task 2 still needs the Swift client endpoint wiring, retry handling, and offline unit tests.";
const rendered = clamped(longText, 240);
const short = conciseReasonText(longText, 140);
const twoSentences = "The latest fix passed checks and updated PR #12. Review and merge, or request another change below.";
console.log(JSON.stringify({{ rendered, short, twoSentencesOut: conciseReasonText(twoSentences, 140) }}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn("Show more", out["rendered"])
        self.assertIn("data-clamp-full hidden", out["rendered"])
        self.assertIn("offline unit tests.", out["rendered"])
        self.assertNotIn("Tas…", out["rendered"])
        self.assertNotIn("Tas…", out["short"])
        self.assertEqual(out["twoSentencesOut"], "The latest fix passed checks and updated PR #12. Review and merge, or request another change below.")

    def test_active_fix_shows_fixing_and_non_ai_actions_omit_parent_job_models(self):
        import os
        import socket
        import subprocess
        import tempfile
        from orchestrator.web import server as ui

        job_failed_build = {
            "status": "debugging",
            "debug_phase": "propose",
            "test_status": "build-failed",
            "debug_history": [{"iteration": 5, "result": {"build_ok": False, "tests_ok": False}}],
        }
        # 1. Stopped (no active run, no alive worker_pid): shows 'Build failing' (needs_you / failed)
        idle_state = ui.job_state(job_failed_build)
        self.assertEqual((idle_state["label"], idle_state["tone"], idle_state["group"]), ("Build failing", "failed", "needs_you"))

        # 2. Active UI fix run OR live worker_pid: shows 'Fixing' (working / working, no Run fix button)
        live_state = ui.job_state(job_failed_build, active_run={"running": True, "waiting": False, "action": "debug"})
        self.assertEqual((live_state["label"], live_state["tone"], live_state["group"], live_state["next"]),
                         ("Fixing", "working", "working", None))
        self.assertIn("previous attempt couldn't build", live_state["reason"])

        pid_state = ui.job_state({**job_failed_build, "worker_pid": os.getpid(), "worker_host": socket.gethostname()})
        self.assertEqual((pid_state["label"], pid_state["tone"], pid_state["group"]), ("Fixing", "working", "working"))

        # 3. Non-AI sessions (setup_environment, install_tool, build, test, server_*, git_*) never inherit assigned_models
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_dir = root / ".orchestrator" / "jobs"
            jobs_dir.mkdir(parents=True)
            (jobs_dir / "j19.json").write_text(
                json.dumps({"status": "debugging", "builder": "gpt-6.2-astra", "planner": "gpt-6.2-astra", "allowed_models": ["gpt-6.2-astra"]}),
                encoding="utf-8",
            )

            def make_session(act_name: str) -> Any:
                sess = ui.PtySession.__new__(ui.PtySession)
                sess.id = f"run-{act_name}"
                sess.action = act_name
                sess.title = act_name
                sess.argv = ["echo", "ok"]
                sess.cwd = root
                sess.job_id = "j19"
                sess.job_title = "Job 19"
                sess.result_job = None
                sess.started = 1000.0
                sess.last_output = 1000.0
                sess.ended = 1002.0
                sess.exit_code = 0
                sess.stopped = False
                sess.result = None
                sess.transcript_path = None
                sess._buffer = bytearray()
                sess._base = 0
                sess._progress = ui.RunProgress()
                sess._timeline = ui.RunTimeline()
                sess._cond = threading.Condition()
                return sess

            for non_ai_act in ("setup_environment", "install_tool", "build", "test", "server_start", "git_push"):
                snap = make_session(non_ai_act).summary()
                self.assertNotIn("assigned_models", snap["progress"], non_ai_act)
            ai_snap = make_session("debug").summary()
            self.assertEqual(ai_snap["progress"].get("assigned_models"), ["gpt-6.2-astra"])

        # 4. Frontend runStatus hides Models row for non-AI actions even if legacy saved progress had assigned_models
        app = (STATIC / "app.js").read_text(encoding="utf-8")
        html_js = (STATIC / "html.js").read_text(encoding="utf-8")
        dur_start = app.index("function runDuration(")
        dur_src = app[dur_start:app.index("function runFailureGuidance(", dur_start)]
        rs_start = app.index("function runStatus(")
        rs_src = app[rs_start:app.index("function wireKeychainUnlock(", rs_start)]
        script = f"""
{html_js}
const esc = globalThis.Html.escape;
const plural = (n, w) => `${{n}} ${{n === 1 ? w : w + "s"}}`;
const act = (a) => `data-action="${{a}}"`;
const stoppingRuns = new Set();
const state = {{}};
const runFailureGuidance = () => null;
const pendingUpdate = null;
{dur_src}
{rs_src}
const out = {{}};
for (const action of ["setup_environment", "install_tool", "build", "test", "server_start", "git_push"]) {{
  out[action] = runStatus({{
    id: `r-${{action}}`,
    action,
    title: action,
    job: "j19",
    running: false,
    exit_code: 0,
    started: 1000,
    ended: 1010,
    progress: {{ assigned_models: ["gpt-6.2-astra"], machines: ["MacBook-Pro"] }},
    result: {{ headline: "Environment ready", details: [], tone: "ok" }},
  }});
}}
console.log(JSON.stringify(out));
"""
        rendered = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        for act_name, html_out in rendered.items():
            self.assertNotIn("gpt-6.2-astra", html_out, act_name)
            self.assertNotIn("<dt>Models</dt>", html_out, act_name)


if __name__ == "__main__":
    unittest.main()
