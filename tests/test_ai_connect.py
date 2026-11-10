"""Connect an AI tool without knowing what a terminal is: install it if needed (trying the other installer when the first
can't run here), run its own sign-in in Orchestrator's terminal, then say plainly whether it worked."""
from __future__ import annotations

import sys
import os
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_providers  # noqa: E402
from orchestrator.scripts import ai_connect  # noqa: E402


class FakeComputer:
    def __init__(self, installed: set[str], tools: set[str] | None = None, signs_in: bool = True, install_fails: set[str] | None = None):
        self.installed, self.tools = set(installed), set(tools or {"bash", "curl"})
        self.signs_in, self.install_fails = signs_in, install_fails or set()
        self.commands: list[list[str]] = []
        self.out: list[str] = []

    def which(self, cli: str) -> str | None:
        return f"/bin/{cli}" if cli in self.installed or cli in self.tools else None

    def run(self, argv: list[str]) -> int:
        self.commands.append(argv)
        if argv[0] in ("bash", "sh") or "install" in " ".join(argv):
            command = argv[-1]
            if any(word in command for word in self.install_fails):
                return 1
            for cli in ("claude", "codex", "pool", "agy"):
                if cli in command:
                    self.installed.add(cli)
            return 0
        return 0

    def ready(self, cli: str) -> bool:
        return self.signs_in and cli in self.installed

    def prerequisites(self, provider: str) -> None:
        self.tools.update({"node", "npm"})

    def connect(self, provider: str) -> int:
        return ai_connect.connect(provider, which=self.which, run=self.run, ready=self.ready, say=self.out.append,
                                  after=lambda: self.out.append("(models refreshed)"),
                                  ensure_prerequisites=self.prerequisites)


class ConnectTests(unittest.TestCase):
    def test_every_sign_in_tool_has_its_command(self):
        self.assertEqual(ai_providers.SIGN_IN_COMMANDS, {"claude": ["auth", "login"], "codex": ["login"], "pool": ["login"], "agy": []})
        for provider_id in ai_providers.SIGN_IN_COMMANDS:
            self.assertIn(provider_id, {p["id"] for p in ai_providers.PROVIDERS})

    def test_installed_tool_goes_straight_to_its_sign_in(self):
        fake = FakeComputer(installed={"claude"})
        self.assertEqual(fake.connect("claude"), 0)
        self.assertEqual(fake.commands, [["/bin/claude", "auth", "login"]])
        self.assertIn("Claude Code (Anthropic) is connected", fake.out[-2])
        self.assertEqual(fake.out[-1], "(models refreshed)")

    def test_a_missing_tool_is_installed_first(self):
        fake = FakeComputer(installed=set())
        self.assertEqual(fake.connect("pool"), 0)
        self.assertEqual(fake.commands[0][0], "bash")
        self.assertIn("poolside", fake.commands[0][-1])
        self.assertEqual(fake.commands[-1], ["/bin/pool", "login"])
        self.assertTrue(any("Installing Poolside" in line for line in fake.out))
        self.assertTrue(any("type y and press Enter" in line for line in fake.out))  # its installer asks to accept its terms
        self.assertTrue(any("already chosen, so press Enter" in line for line in fake.out))  # and its sign-in asks how first

    def test_codex_uses_user_local_npm_even_when_homebrew_is_available(self):
        # Preparing npm avoids a system-wide Homebrew install.
        fake = FakeComputer(installed=set(), tools={"bash", "curl", "brew"})
        self.assertEqual(fake.connect("codex"), 0)
        self.assertEqual(fake.commands[0][-1], "npm install -g @openai/codex@latest")
        self.assertEqual(fake.commands[-1], ["/bin/codex", "login"])

    def test_missing_node_is_installed_automatically(self):
        fake = FakeComputer(installed=set(), tools={"bash", "curl"})
        self.assertEqual(fake.connect("codex"), 0)
        self.assertIn("npm install", fake.commands[0][-1])
        self.assertIn("Node.js", " ".join(fake.out))

    def test_progress_reports_each_stage_in_order(self):
        fake = FakeComputer(installed=set())
        events = []
        result = ai_connect.connect("codex", which=fake.which, run=fake.run, ready=fake.ready,
                                    say=fake.out.append, after=lambda: None,
                                    ensure_prerequisites=fake.prerequisites,
                                    progress=lambda step, detail: events.append((step, detail)))
        self.assertEqual(result, 0)
        self.assertEqual([step for step, detail in events], ["check", "prerequisites", "install", "sign_in", "ready"])
        self.assertTrue(all(detail for step, detail in events))

    def test_prerequisite_failure_is_actionable_and_stops_install(self):
        fake = FakeComputer(installed=set())
        def fail(provider):
            raise RuntimeError("Node.js download checksum did not match")
        result = ai_connect.connect("codex", which=fake.which, run=fake.run, ready=fake.ready,
                                    say=fake.out.append, after=lambda: None, ensure_prerequisites=fail)
        self.assertEqual(result, 1)
        self.assertFalse(fake.commands)
        self.assertIn("checksum", " ".join(fake.out))

    def test_failed_process_start_emits_structured_error(self):
        fake = FakeComputer(installed={"codex"})
        events = []
        def broken(argv):
            raise OSError("executable was removed")
        result = ai_connect.connect("codex", which=fake.which, run=broken, ready=fake.ready,
                                    say=fake.out.append, after=lambda: None,
                                    progress=lambda step, detail: events.append((step, detail)))
        self.assertEqual(result, 1)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("removed", events[-1][1])

    def test_native_installer_does_not_bootstrap_node(self):
        fake = FakeComputer(installed=set())
        def forbidden(provider):
            self.fail("Native curl installers must not install Node")
        self.assertEqual(ai_connect.connect("claude", which=fake.which, run=fake.run,
            ready=fake.ready, say=fake.out.append, after=lambda: None, ensure_prerequisites=forbidden), 0)

    def test_a_sign_in_that_did_not_finish_says_so(self):
        fake = FakeComputer(installed={"codex"}, signs_in=False)
        self.assertEqual(fake.connect("codex"), 1)
        self.assertIn("isn't signed in yet", " ".join(fake.out))
        self.assertNotIn("(models refreshed)", fake.out)

    def test_a_tool_without_a_sign_in_is_refused(self):
        with self.assertRaises(ValueError):
            FakeComputer(installed=set()).connect("gemini")


class MainTests(unittest.TestCase):
    def test_fresh_mac_install_keeps_npm_local_and_emits_machine_readable_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            local_bin = Path(directory) / ".local/bin"
            def executable(name):
                local_bin.mkdir(parents=True, exist_ok=True)
                path = local_bin / name
                path.write_text("#!/bin/sh\nexit 0\n")
                path.chmod(0o755)
            def bootstrap():
                executable("node")
                executable("npm")
            commands = []
            def call(argv, *, env):
                commands.append(argv)
                self.assertEqual(env["npm_config_prefix"], str(Path(directory) / ".local"))
                self.assertIn(str(local_bin), env["PATH"].split(os.pathsep))
                if argv[0] == "bash":
                    self.assertEqual(argv[1], "-c")
                    executable("codex")
                return 0
            output = io.StringIO()
            with patch.dict(os.environ, {"HOME": directory, "PATH": "/usr/bin:/bin",
                            "ORCHESTRATOR_TOOL_DIRS": str(local_bin)}), \
                    patch("orchestrator.tool_prerequisites.install_node", side_effect=bootstrap), \
                    patch("orchestrator.scripts.ai_connect.subprocess.call", side_effect=call), \
                    patch("orchestrator.setup_checklist._cli_ready", return_value=True), \
                    patch("orchestrator.project_setup.refresh_ready_models"), redirect_stdout(output):
                self.assertEqual(ai_connect.main(["--provider", "codex"]), 0)
            events = [json.loads(line.removeprefix("@@ORCHESTRATOR_CONNECT@@"))
                      for line in output.getvalue().splitlines() if line.startswith("@@ORCHESTRATOR_CONNECT@@")]
            self.assertEqual([event["step"] for event in events],
                             ["check", "prerequisites", "install", "sign_in", "ready"])
            self.assertEqual(commands[-1], [str(local_bin / "codex"), "login"])


class InteractiveSignInTests(unittest.TestCase):
    def test_observed_readiness_finishes_only_the_owned_login_process(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "signed-in"
            script = "from pathlib import Path; import time; Path(" + repr(str(marker)) + ").touch(); time.sleep(30)"
            result = ai_connect.run_interactive_sign_in([sys.executable, "-c", script],
                env=dict(os.environ), ready=lambda: marker.exists(), poll_interval=0.01)
            self.assertEqual(result, 0)
            self.assertTrue(marker.exists())

    def test_sign_in_exit_without_readiness_keeps_its_failure(self):
        self.assertEqual(ai_connect.run_interactive_sign_in([sys.executable, "-c", "raise SystemExit(7)"],
            env=dict(os.environ), ready=lambda: False, poll_interval=0.01), 7)


class WiringTests(unittest.TestCase):
    def test_connect_runs_in_a_terminal_for_the_owner_and_the_page_offers_it(self):
        from orchestrator.web import server
        self.assertIn("ai_connect", server.TERMINAL_ACTIONS)  # sign-ins ask questions: it takes typing
        self.assertIn("ai_connect", server.OWNER_ONLY_ACTIONS)
        argv = server.build_ai_connect({"provider": "codex"})
        self.assertIn("ai_connect.py", argv)
        self.assertEqual(argv[-2:], ["--provider", "codex"])
        with self.assertRaises(server.UIError):
            server.build_ai_connect({"provider": "gemini"})  # key only: nothing to sign in to
        state = ai_providers.with_status(lambda cli: False, lambda cli: False, set())
        self.assertEqual({p["id"] for p in state["providers"] if p["can_connect"]}, {"claude", "codex", "pool", "agy"})
        root = Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static"
        self.assertIn('p.id === "opencode" && owner ?', (root / "configuration.js").read_text())
        self.assertIn('if (event.target.closest("[data-free-ai]")) { setUpFreeAi(); return; }', (root / "app.js").read_text())
        self.assertIn('data-ai-connect="${escapeHtml(p.id)}"', (root / "configuration.js").read_text())
        self.assertIn('ToolConnect.open(button.dataset.aiConnect, button.dataset.aiName', (root / "app.js").read_text())


if __name__ == "__main__":
    unittest.main()
