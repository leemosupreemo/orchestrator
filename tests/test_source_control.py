from __future__ import annotations

import unittest

from orchestrator import source_control as sc


class OptionsTests(unittest.TestCase):
    def test_every_option_explains_itself_completely(self):
        self.assertEqual([o["key"] for o in sc.OPTIONS], ["github", "gitlab", "bitbucket", "azure", "other", "local"])
        for o in sc.OPTIONS:
            for field in ("label", "tagline", "workflow", "docs"):
                self.assertTrue(o[field], (o["key"], field))
            self.assertGreaterEqual(len(o["good"]), 2, o["key"])  # strengths
            self.assertGreaterEqual(len(o["watch"]), 2, o["key"])  # and weaknesses
            self.assertGreaterEqual(len(o["steps"]), 3, o["key"])  # and the steps
            self.assertIn(o["action"], ("github", "connect", "local"))
            self.assertTrue(o["docs"].startswith("https://"))
        self.assertEqual(sc.BY_KEY["github"]["action"], "github")
        self.assertEqual(sc.BY_KEY["local"]["action"], "local")

    def test_steps_name_the_buttons_that_exist_on_the_page(self):
        for key, buttons in (("github", ("Sign in to GitHub", "Create a GitHub repository")), ("gitlab", ("Connect a repository",)),
                             ("local", ("Initialize repository", "Use this computer only"))):
            text = " ".join(sc.BY_KEY[key]["steps"])
            for button in buttons:
                self.assertIn(button, text, key)

    def test_the_blurb_is_a_topline_reason(self):
        self.assertTrue(sc.BLURB.startswith("Source control keeps a history"))
        self.assertLess(len(sc.BLURB), 400)


class CurrentKeyTests(unittest.TestCase):
    def test_a_remote_decides_it(self):
        self.assertEqual(sc.current_key("github", "git@github.com:a/b.git", None), "github")
        self.assertEqual(sc.current_key("gitlab", "git@gitlab.com:a/b.git", "auto"), "gitlab")
        self.assertEqual(sc.current_key("azure", "https://dev.azure.com/o/p/_git/r", None), "azure")
        self.assertEqual(sc.current_key("other", "git@git.example.com:a/b.git", None), "other")

    def test_no_remote_is_local_only_once_chosen_and_otherwise_not_chosen(self):
        self.assertEqual(sc.current_key("none", "", "git"), "local")
        self.assertEqual(sc.current_key("none", "", "auto"), "")
        self.assertEqual(sc.current_key("none", "", None), "")


class HelpPromptTests(unittest.TestCase):
    FACTS = {"is_repo": True, "has_commits": False, "origin": "", "project": "Snake", "stack": "Swift (Xcode)", "system": "a Mac",
             "github": {"installed": True, "user": ""}}

    def test_the_prompt_starts_from_where_the_project_is(self):
        text = sc.help_prompt("github", self.FACTS)
        for expected in ("chose: GitHub", "is a Git repository, with no commits yet", "Remote ('origin'): none", "Project: Snake, Swift (Xcode)", "a Mac",
                         "installed, not signed in", "at most 8 steps", "Sign in to GitHub", "Don't invent web addresses"):
            self.assertIn(expected, text)

    def test_the_github_line_only_appears_for_github(self):
        self.assertNotIn("GitHub CLI", sc.help_prompt("gitlab", self.FACTS))
        self.assertIn("signed in as octo", sc.help_prompt("github", {**self.FACTS, "github": {"installed": True, "user": "octo"}}))
        self.assertIn("not installed", sc.help_prompt("github", {**self.FACTS, "github": {}}))

    def test_an_unknown_option_is_refused(self):
        with self.assertRaises(ValueError):
            sc.help_prompt("svn", self.FACTS)


if __name__ == "__main__":
    unittest.main()
