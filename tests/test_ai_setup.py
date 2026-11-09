"""Getting a free AI working in one step, for someone who has never used a command line: check, install OpenCode if it's
missing, make sure its free models answer, then turn them on for jobs, saying which step it's on throughout."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_providers, ai_setup  # noqa: E402

KNOWN = ["opencode/nemotron-3-ultra-free", "opencode/deepseek-v4-flash-free", "opencode/qwen-3.7-max"]


class Recorder:
    def __init__(self):
        self.steps: list[tuple[str, str]] = []

    def update(self, stage: str, detail: str = "") -> None:
        self.steps.append((stage, detail))


class FakeComputer:
    """OpenCode present or not, an installer that works or doesn't, and what `opencode models` lists."""

    def __init__(self, installed: bool, install_works: bool = True, offered: list[str] | None = None):
        self.installed, self.install_works = installed, install_works
        self.offered = offered if offered is not None else ["opencode/nemotron-3-ultra-free", "opencode/big-pickle"]
        self.installs, self.refreshed = [], []

    def which(self, cli: str) -> str | None:
        return f"/home/me/.opencode/bin/{cli}" if self.installed and cli == "opencode" else None

    def install(self, provider: str) -> dict:
        self.installs.append(provider)
        if not self.install_works:
            raise RuntimeError("Couldn't install OpenCode. curl: (6) Could not resolve host")
        self.installed = True
        return {"provider": "OpenCode"}

    def models(self, path: str) -> list[str]:
        return list(self.offered)

    def run(self, root: Path, progress=None) -> dict:
        return ai_setup.ensure_free_ai(root, progress, which=self.which, install=self.install, list_models=self.models,
                                       known=KNOWN, refresh=self.refreshed.append)


class ToolFolderTests(unittest.TestCase):
    def test_tools_are_looked_for_where_their_installers_put_them(self):
        path = ai_providers.tool_path("/usr/bin").split(":")
        self.assertEqual(path[0], "/usr/bin")
        for folder in (str(Path.home() / ".opencode" / "bin"), str(Path.home() / ".local" / "bin"), "/opt/homebrew/bin"):
            self.assertIn(folder, path)


class FreeAiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_already_installed_is_checked_and_turned_on_without_installing(self):
        fake, progress = FakeComputer(installed=True), Recorder()
        result = fake.run(self.root, progress)
        self.assertEqual(fake.installs, [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "opencode/nemotron-3-ultra-free")
        self.assertFalse(result["installed_now"])
        self.assertEqual([s for s, _ in progress.steps], ["check", "verify", "ready"])
        self.assertEqual(fake.refreshed, [self.root])  # the computer's models follow at once, so setup ticks off

    def test_missing_opencode_is_installed_then_tested(self):
        fake, progress = FakeComputer(installed=False), Recorder()
        result = fake.run(self.root, progress)
        self.assertEqual(fake.installs, ["opencode"])
        self.assertTrue(result["ok"] and result["installed_now"])
        self.assertEqual([s for s, _ in progress.steps], ["check", "install", "verify", "ready"])
        self.assertTrue(all(detail for _, detail in progress.steps))  # every step says something in plain words

    def test_an_installer_that_fails_says_why_and_offers_poolside(self):
        result = FakeComputer(installed=False, install_works=False).run(self.root, Recorder())
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "install")
        self.assertIn("Could not resolve host", result["message"])
        self.assertEqual(result["fallback"]["id"], "pool")

    def test_no_free_model_offered_is_a_failure_not_a_paid_surprise(self):
        fake = FakeComputer(installed=True, offered=["opencode/qwen-3.7-max"])  # only a paid one
        result = fake.run(self.root, Recorder())
        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "verify")
        self.assertEqual(result["fallback"]["id"], "pool")
        self.assertEqual(fake.refreshed, [])

    def test_free_models_orchestrator_hasnt_heard_of_yet_are_learned_then_used(self):
        # OpenCode changes its free line-up often: what it offers may not be in Orchestrator's list until it's refreshed
        fake, progress = FakeComputer(installed=True, offered=["opencode/beam-free", "opencode/big-pickle"]), Recorder()
        learned = []
        known = lambda: KNOWN + (["opencode/beam-free"] if learned else [])  # noqa: E731
        result = ai_setup.ensure_free_ai(self.root, progress, which=fake.which, install=fake.install, list_models=fake.models,
                                         known=known, refresh_known=lambda: learned.append(True), refresh=fake.refreshed.append)
        self.assertEqual(learned, [True])
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["model"], "opencode/beam-free")
        self.assertIn("Updating", " ".join(d for _, d in progress.steps))

    def test_no_refresh_when_the_known_list_already_has_one(self):
        learned = []
        fake = FakeComputer(installed=True)
        ai_setup.ensure_free_ai(self.root, Recorder(), which=fake.which, install=fake.install, list_models=fake.models,
                                known=KNOWN, refresh_known=lambda: learned.append(True), refresh=fake.refreshed.append)
        self.assertEqual(learned, [])

    def test_the_steps_have_names_for_the_page(self):
        self.assertEqual([k for k, _ in ai_setup.STEPS], ["check", "install", "verify", "ready"])


if __name__ == "__main__":
    unittest.main()
