import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import code_host


class CodeHostTests(unittest.TestCase):
    def test_parses_https_ssh_and_scp_style_remotes(self):
        cases = {
            "https://github.com/me/app.git": ("github.com", "me/app"),
            "git@github.com:me/app.git": ("github.com", "me/app"),
            "ssh://git@gitlab.example.com:2222/group/sub/app.git": ("gitlab.example.com", "group/sub/app"),
            "https://user@bitbucket.org/team/app.git": ("bitbucket.org", "team/app"),
            "git@ssh.dev.azure.com:v3/org/proj/app": ("ssh.dev.azure.com", "v3/org/proj/app"),
            "https://gitea.home.lan/me/app/": ("gitea.home.lan", "me/app"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(code_host.parse_remote(url), expected)
        self.assertIsNone(code_host.parse_remote(""))
        self.assertIsNone(code_host.parse_remote("/srv/git/app.git"))

    def test_host_kinds(self):
        self.assertEqual(code_host.host_kind("git@github.com:me/app.git"), "github")
        self.assertEqual(code_host.host_kind("https://gitlab.com/me/app.git"), "gitlab")
        self.assertEqual(code_host.host_kind("git@gitlab.company.io:me/app.git"), "gitlab")
        self.assertEqual(code_host.host_kind("git@bitbucket.org:team/app.git"), "bitbucket")
        self.assertEqual(code_host.host_kind("https://dev.azure.com/org/proj/_git/app"), "azure")
        self.assertEqual(code_host.host_kind("https://gitea.home.lan/me/app"), "other")
        self.assertEqual(code_host.host_kind(""), "none")

    def test_mode_follows_origin_unless_set(self):
        self.assertEqual(code_host.resolve_mode(None, "git@github.com:me/app.git"), "github")
        self.assertEqual(code_host.resolve_mode("auto", "https://gitlab.com/me/app.git"), "git")
        self.assertEqual(code_host.resolve_mode(None, ""), "git")
        self.assertEqual(code_host.resolve_mode("git", "git@github.com:me/app.git"), "git")  # GitHub, but without issues and PRs
        self.assertEqual(code_host.resolve_mode("GitHub", ""), "github")

    def test_review_request_links(self):
        self.assertEqual(code_host.review_request_url("git@gitlab.com:me/app.git", "ai/job-fix-1", "main"),
                         "https://gitlab.com/me/app/-/merge_requests/new?merge_request%5Bsource_branch%5D=ai%2Fjob-fix-1"
                         "&merge_request%5Btarget_branch%5D=main")
        self.assertEqual(code_host.review_request_url("https://bitbucket.org/team/app.git", "ai/job-x", "develop"),
                         "https://bitbucket.org/team/app/pull-requests/new?source=ai%2Fjob-x&dest=develop")
        self.assertEqual(code_host.review_request_url("git@github.com:me/app.git", "ai/job-x", "main"),
                         "https://github.com/me/app/compare/main...ai%2Fjob-x?expand=1")
        self.assertEqual(code_host.review_request_url("https://gitea.home.lan/me/app", "b", "main"), "")
        self.assertEqual(code_host.review_request_url("", "b", "main"), "")

    def test_origin_url_reads_the_repository(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(code_host.origin_url(root), "")
            subprocess.run(["git", "init", "-q"], cwd=folder, check=True)
            self.assertEqual(code_host.origin_url(root), "")
            subprocess.run(["git", "remote", "add", "origin", "git@gitlab.com:me/app.git"], cwd=folder, check=True)
            self.assertEqual(code_host.origin_url(root), "git@gitlab.com:me/app.git")


if __name__ == "__main__":
    unittest.main()
