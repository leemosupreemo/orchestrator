"""Connect an AI tool without knowing what a terminal is: install it if needed (trying the other installer when the first
can't run here), run its own sign-in in Orchestrator's terminal, then say plainly whether it worked."""
from __future__ import annotations

import sys
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

    def connect(self, provider: str) -> int:
        return ai_connect.connect(provider, which=self.which, run=self.run, ready=self.ready, say=self.out.append,
                                  after=lambda: self.out.append("(models refreshed)"))


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

    def test_when_the_installer_needs_something_missing_the_other_one_is_used(self):
        # Codex installs with npm; a new Mac has no Node, but with Homebrew there's the other way
        fake = FakeComputer(installed=set(), tools={"bash", "curl", "brew"})
        self.assertEqual(fake.connect("codex"), 0)
        self.assertEqual(fake.commands[0][-1], "brew install --cask codex")
        self.assertEqual(fake.commands[-1], ["/bin/codex", "login"])

    def test_with_neither_it_says_what_to_install_in_plain_words(self):
        fake = FakeComputer(installed=set(), tools={"bash", "curl"})
        self.assertEqual(fake.connect("codex"), 1)
        self.assertEqual(fake.commands, [])
        self.assertIn("Node.js", " ".join(fake.out))

    def test_a_sign_in_that_did_not_finish_says_so(self):
        fake = FakeComputer(installed={"codex"}, signs_in=False)
        self.assertEqual(fake.connect("codex"), 1)
        self.assertIn("isn't signed in yet", " ".join(fake.out))
        self.assertNotIn("(models refreshed)", fake.out)

    def test_a_tool_without_a_sign_in_is_refused(self):
        with self.assertRaises(ValueError):
            FakeComputer(installed=set()).connect("gemini")


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
        self.assertIn('runAction("ai_connect", { provider: button.dataset.aiConnect', (root / "app.js").read_text())


if __name__ == "__main__":
    unittest.main()
