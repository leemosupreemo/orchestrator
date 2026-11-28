"""`orchestrator sandbox`: Orchestrator as on a brand-new Mac, in a throwaway home folder, to go through setup again and
again without touching the real setup."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_providers, sandbox  # noqa: E402


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_it_lives_in_its_own_folder_with_its_own_home(self):
        home = sandbox.prepare("trial", base=self.base)
        self.assertEqual(home, self.base / "trial" / "home")
        self.assertTrue(home.is_dir())
        with self.assertRaises(ValueError):
            sandbox.prepare("../escape", base=self.base)

    def test_the_environment_hides_this_computers_setup_and_tools(self):
        home = sandbox.prepare("trial", base=self.base)
        env = sandbox.environment(home, base_env={"PATH": "/opt/homebrew/bin:/usr/bin", "HOME": "/Users/me",
                                                  "ANTHROPIC_API_KEY": "secret", "ORCHESTRATOR_CONFIG": "/x", "LANG": "en_US.UTF-8"})
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")  # no Homebrew, no tools you installed
        self.assertEqual(env["ORCHESTRATOR_TOOL_DIRS"], os.pathsep.join([str(home / ".opencode" / "bin"), str(home / ".local" / "bin")]))
        self.assertNotIn("ANTHROPIC_API_KEY", env)  # no AI keys from your shell
        self.assertNotIn("ORCHESTRATOR_CONFIG", env)
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(env["ORCHESTRATOR_SANDBOX"], str(home))

    def test_keep_tools_lets_it_see_what_is_installed(self):
        home = sandbox.prepare("trial", base=self.base)
        env = sandbox.environment(home, keep_tools=True, base_env={"PATH": "/opt/homebrew/bin:/usr/bin"})
        self.assertEqual(env["PATH"], "/opt/homebrew/bin:/usr/bin")
        self.assertNotIn("ORCHESTRATOR_TOOL_DIRS", env)

    def test_tool_lookups_use_only_the_sandboxs_folders(self):
        old = os.environ.get("ORCHESTRATOR_TOOL_DIRS")
        os.environ["ORCHESTRATOR_TOOL_DIRS"] = "/sandbox/.opencode/bin:/sandbox/.local/bin"
        try:
            self.assertEqual(ai_providers.tool_path("/usr/bin"), "/usr/bin:/sandbox/.opencode/bin:/sandbox/.local/bin")
        finally:
            if old is None:
                os.environ.pop("ORCHESTRATOR_TOOL_DIRS", None)
            else:
                os.environ["ORCHESTRATOR_TOOL_DIRS"] = old

    def test_reset_and_wipe_start_over(self):
        home = sandbox.prepare("trial", base=self.base)
        (home / ".orchestrator").mkdir()
        (home / ".orchestrator" / "projects.json").write_text("[]")
        home = sandbox.prepare("trial", base=self.base, reset=True)
        self.assertEqual(list(home.iterdir()), [])
        self.assertTrue(sandbox.wipe("trial", base=self.base))
        self.assertFalse((self.base / "trial").exists())
        self.assertFalse(sandbox.wipe("trial", base=self.base))  # nothing there: says so

    def test_a_sample_project_to_add(self):
        home = sandbox.prepare("trial", base=self.base)
        project = sandbox.sample_project(home)
        self.assertEqual(project, home / "Projects" / "sample-app")
        self.assertTrue((project / ".git").is_dir())
        self.assertTrue((project / "README.md").is_file())
        log = subprocess.run(["git", "log", "--oneline"], cwd=project, capture_output=True, text=True).stdout
        self.assertEqual(len(log.splitlines()), 1)

    def test_it_starts_like_a_brand_new_install_asking_for_a_project(self):
        home = sandbox.prepare("trial", base=self.base)
        server = sandbox.start_server(home, port=0, token="t0k", env=sandbox.environment(home))
        try:
            port = sandbox.wait_for_port(server, timeout=20)
            request = urllib.request.Request(f"http://127.0.0.1:{port}/api/state", headers={"Authorization": "Bearer t0k"})
            with urllib.request.urlopen(request, timeout=10) as reply:
                state = json.loads(reply.read())
            self.assertTrue(state["needs_project"])
            self.assertEqual(state["projects"], [])
        finally:
            server.terminate()
            server.wait(10)
        self.assertFalse((Path.home() / ".orchestrator" / "trial").exists())  # nothing written to the real home


if __name__ == "__main__":
    unittest.main()
