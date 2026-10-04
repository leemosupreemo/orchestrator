"""Runs started from the web app never wait at a terminal: prompts take defaults, "press Enter" pauses don't wait,
and anything that needs typed input closes cleanly so the question can go to the app instead."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator.web import server as ui  # noqa: E402

PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import common
out = {"interactive": common.interactive(),
       "radio": common.prompt_radio("Branch:", ["new", "current"], "current"),
       "checkbox": common.prompt_checkbox("Models", ["a", "b", "c"], ["b"]),
       "confirm": common.prompt_confirm("Sure?", default=False),
       "enter": input("Tap Enter to return to menu...")}
for name, call in (("input", lambda: input("Your Answer:")), ("multiline", lambda: common.prompt_multiline("Vision:"))):
    try:
        call(); out[name] = "answered"
    except EOFError:
        out[name] = "closed"
print(json.dumps(out))
"""


class PromptsInWebRunsTests(unittest.TestCase):
    def run_in_pty(self, code: str, noninteractive: bool):
        """Like the web app's runs: stdin is a pseudo-terminal, so isatty() is true."""
        import pty
        env = {**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)}
        env.pop("ORCHESTRATOR_NONINTERACTIVE", None)
        if noninteractive:
            env["ORCHESTRATOR_NONINTERACTIVE"] = "1"
        primary, secondary = pty.openpty()
        try:
            res = subprocess.run([sys.executable, "-c", code, str(PACKAGE_ROOT / "orchestrator" / "scripts")], cwd=PACKAGE_ROOT,
                                 env=env, capture_output=True, text=True, stdin=secondary, timeout=60)
        finally:
            os.close(primary)
            os.close(secondary)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_a_terminal_without_the_marker_counts_as_interactive(self):
        printed = self.run_in_pty("import sys; sys.path.insert(0, sys.argv[1]); import common; print(common.interactive())", noninteractive=False)
        self.assertEqual(printed.strip(), "True")  # which is why web runs need the marker

    def test_a_web_run_takes_defaults_and_never_waits(self):
        printed = self.run_in_pty(PROBE, noninteractive=True)
        out = json.loads(printed.strip().splitlines()[-1])
        self.assertEqual(out, {"interactive": False, "radio": "current", "checkbox": ["b"], "confirm": False, "enter": "",
                               "input": "closed", "multiline": "closed"})
        self.assertIn("needs an answer typed in a terminal", printed)  # says why instead of hanging


class WebRunEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestrator").mkdir()
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_name": "Demo"}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_web_runs_are_marked_noninteractive_except_the_console(self):
        with patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(self.root / "state")}):
            server = ui.UIServer(("127.0.0.1", 0), self.root, token="t")
            try:
                self.assertEqual(server.child_env()["ORCHESTRATOR_NONINTERACTIVE"], "1")
                self.assertNotIn("ORCHESTRATOR_NONINTERACTIVE", server.child_env(terminal=True))
            finally:
                server.server_close()
        self.assertEqual(ui.TERMINAL_ACTIONS, {"console", "wizard", "config_menu"})

    def test_device_logs_setup_never_prompts_and_keeps_the_token_out_of_the_command_line(self):
        params = {"token": "sntryu_secret", "org": "acme", "api_base": "https://us.sentry.io"}
        argv = ui.ACTIONS["logs_setup"].build(params, self.root)
        self.assertIn("--no-prompt", argv)
        self.assertIn("--save-token", argv)
        self.assertNotIn("sntryu_secret", " ".join(argv))
        self.assertEqual(ui.ACTIONS["logs_setup"].env(params), {"SENTRY_AUTH_TOKEN": "sntryu_secret"})


class ReviseKeepsTheJobsChoicesTests(unittest.TestCase):
    def test_revise_passes_models_machines_and_branch_mode(self):
        sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))
        import job_actions
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "job.json"
            path.write_text(json.dumps({"type": "feature-plan", "allowed_models": ["gpt-5.5", "opencode/big-pickle"],
                                        "allowed_machines": ["local"], "branch_mode": "manual"}))
            with patch.object(job_actions, "run_script", return_value=0) as run:
                job_actions.revise(path, "Smaller tasks", "", "")
        args = run.call_args[0][1]
        for flag, value in (("--allowed-models", "gpt-5.5,opencode/big-pickle"), ("--allowed-machines", "local"), ("--branch-mode", "manual")):
            self.assertEqual(args[args.index(flag) + 1], value)


class TheAppNeverOpensTerminalMenusTests(unittest.TestCase):
    def test_only_the_sidebar_console_starts_a_terminal_tool(self):
        static = PACKAGE_ROOT / "orchestrator" / "web" / "static"
        app = (static / "app.js").read_text() + (static / "configuration.js").read_text()
        for tool in ("wizard", "config_menu", "console"):
            self.assertNotIn(f'runAction("{tool}"', app, tool)
            self.assertNotIn(f'act("{tool}"', app, tool)
        self.assertIn('data-action="console"', (static / "index.html").read_text())  # the one labelled way in
        self.assertIn("async logs_setup()", app)  # device logs are set up in a form, not terminal questions
        self.assertIn("async function signInToGitHub()", app)


class SetupChecklistStaysInTheAppTests(unittest.TestCase):
    def test_no_setup_item_opens_a_terminal_tool(self):
        from orchestrator.setup_checklist import setup_checklist
        with tempfile.TemporaryDirectory() as tmp:
            items = setup_checklist(Path(tmp), Path(tmp) / ".orchestrator")["items"]
        for item in items:
            action = item.get("action") or {}
            self.assertNotIn(action.get("action"), ui.TERMINAL_ACTIONS, item["id"])


class JobsNeverSendYouToTheConsoleTests(unittest.TestCase):
    def test_no_job_state_offers_the_console(self):
        base = {"type": "feature-plan", "plan": {"tasks": [{"title": "a"}, {"title": "b"}]}, "completed_tasks": [0]}
        for status in ("planned", "designing", "human-needed", "scheduled", "executing", "review-needed", "debugging",
                       "failed", "completed", "something-new"):
            for extra in ({}, {"last_error": "boom"}, {"human_clarification_question": "Which one?"}):
                st = ui.job_state({**base, "status": status, **extra})
                self.assertNotEqual((st["next"] or {}).get("action"), "console", (status, extra))

    def test_a_job_that_stopped_with_an_error_says_so_and_offers_to_try_again(self):
        st = ui.job_state({"type": "feature-plan", "status": "human-needed", "last_error": "No module named 'x'",
                           "plan": {"tasks": [{"title": "a"}]}})
        self.assertEqual((st["label"], st["next"]["action"], st["next"]["label"]), ("Stopped", "resume", "Try again"))
        self.assertIn("No module named 'x'", st["reason"])


class OrchestratorsOwnFolderTests(unittest.TestCase):
    def test_jobs_cant_switch_branches_in_the_folder_orchestrator_runs_from(self):
        from orchestrator import run_check
        self.assertTrue(run_check.runs_from(PACKAGE_ROOT))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(run_check.runs_from(tmp))
        self.assertTrue(run_check.switches_branches({"branch_mode": "new"}))
        self.assertFalse(run_check.switches_branches({"branch_mode": "manual"}))  # no git changes: allowed
        blockers = ui.job_blockers(PACKAGE_ROOT, {"status": "planned", "branch_mode": "new"})
        self.assertEqual(blockers[0]["id"], "self_checkout")


if __name__ == "__main__":
    unittest.main()
