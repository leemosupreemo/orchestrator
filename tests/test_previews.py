"""Web project previews: the local live preview's dev server, and the job page's preview."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unittest import mock  # noqa: E402

from orchestrator.scripts import dev_preview  # noqa: E402
from orchestrator.web import server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class DevServerTests(unittest.TestCase):
    def test_the_address_a_dev_server_prints_becomes_a_link(self):
        self.assertEqual(dev_preview.address("  ➜  Local:   \x1b[36mhttp://localhost:5173/\x1b[39m"), "http://localhost:5173/")
        self.assertEqual(dev_preview.address("ready - started server on 0.0.0.0:3000, url: http://0.0.0.0:3000"), "http://localhost:3000")
        self.assertEqual(dev_preview.address("Serving HTTP on 127.0.0.1 port 8123 (http://127.0.0.1:8123/) ..."), "http://127.0.0.1:8123/")
        self.assertEqual(dev_preview.address("compiled successfully"), "")

    def test_it_runs_the_projects_dev_script_installing_packages_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "package.json").write_text(json.dumps({"scripts": {"start": "x", "dev": "vite"}}))
            (folder / "package-lock.json").write_text("{}")
            setup, server = dev_preview.dev_commands(folder, {})
            self.assertEqual(setup, [["npm", "ci"]])
            self.assertEqual(server, ["npm", "run", "dev"])
            (folder / "node_modules").mkdir()
            self.assertEqual(dev_preview.dev_commands(folder, {})[0], [])
            self.assertEqual(dev_preview.dev_commands(folder, {"preview_dev_command": "make serve"}),
                             ([], ["/bin/sh", "-c", "make serve"]))

    def test_it_uses_the_projects_package_manager_and_never_writes_a_lockfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
            self.assertEqual(dev_preview.dev_commands(folder, {}), ([["npm", "install", "--no-package-lock"]], ["npm", "run", "dev"]))
            (folder / "yarn.lock").write_text("")
            self.assertEqual(dev_preview.dev_commands(folder, {}), ([["yarn", "install", "--frozen-lockfile"]], ["yarn", "run", "dev"]))
            (folder / "pnpm-lock.yaml").write_text("")
            self.assertEqual(dev_preview.dev_commands(folder, {})[1], ["pnpm", "run", "dev"])

    def test_a_branch_that_isnt_checked_out_runs_from_outside_the_project(self):
        root = Path("/Users/me/site")
        folder = dev_preview.copy_folder(root, "feature-x")
        self.assertNotIn(root, folder.parents)
        self.assertEqual(folder.name, "feature-x")
        self.assertNotEqual(folder, dev_preview.copy_folder(Path("/Users/me/other/site"), "feature-x"))

    def test_a_site_without_a_build_is_served_as_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            setup, server = dev_preview.dev_commands(Path(tmp), {})
            self.assertEqual(setup, [])
            self.assertIn("http.server", server)

    def test_a_package_without_a_dev_script_says_how_to_set_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "package.json").write_text(json.dumps({"scripts": {"build": "x"}}))
            with self.assertRaisesRegex(ValueError, "preview_dev_command"):
                dev_preview.dev_commands(Path(tmp), {})

    def test_it_finds_where_a_branch_is_checked_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            run = lambda *a, cwd=repo: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)  # noqa: E731
            run("init", "-q", "-b", "main")
            run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "first")
            run("branch", "feature")
            self.assertEqual(dev_preview.checked_out_in(repo, "main").resolve(), repo.resolve())
            self.assertIsNone(dev_preview.checked_out_in(repo, "feature"))
            run("worktree", "add", "-q", str(Path(tmp) / "wt"), "feature")
            self.assertEqual(dev_preview.checked_out_in(repo, "feature").resolve(), (Path(tmp) / "wt").resolve())


def web_project(tmp: str) -> Path:
    """A web project with Firebase Hosting, a job on its own branch that's ready for review."""
    root = Path(tmp) / "site"
    root.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)  # noqa: E731
    run("init", "-q", "-b", "main")
    (root / "index.html").write_text("<h1>hi</h1>")
    (root / "firebase.json").write_text(json.dumps({"hosting": {"public": "."}}))
    (root / ".firebaserc").write_text(json.dumps({"projects": {"default": "demo"}}))
    run("add", ".")
    run("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "first")
    run("branch", "job-1-branch")
    (root / ".orchestrator" / "jobs").mkdir(parents=True)
    (root / ".orchestrator" / "jobs" / "job-1.json").write_text(json.dumps({"id": "job-1", "title": "Header", "status": "review-needed",
                                                                         "branch": "job-1-branch"}))
    return root


class JobPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = web_project(self.tmp.name)
        self.firebase = mock.patch.object(server.shutil, "which", lambda name: f"/usr/bin/{name}")
        self.firebase.start()

    def tearDown(self):
        self.firebase.stop()
        self.tmp.cleanup()

    def publish(self, commit: str) -> None:
        out = self.root / ".orchestrator" / "output" / "previews"
        out.mkdir(parents=True, exist_ok=True)
        (out / "job-1-branch.json").write_text(json.dumps({"branch": "job-1-branch", "commit": commit, "url": "https://demo--job-1-branch-x.web.app", "at": 1}))

    def test_a_job_ready_for_review_publishes_only_when_automatic_previews_are_on(self):
        self.assertEqual(server.auto_preview_branch(self.root, "job-1"), "")
        server.write_settings(self.root, {"auto_preview": True})
        self.assertEqual(server.auto_preview_branch(self.root, "job-1"), "job-1-branch")

    def test_it_doesnt_publish_again_when_the_link_is_at_the_branchs_latest_commit(self):
        server.write_settings(self.root, {"auto_preview": True})
        self.publish(server.git(self.root, "rev-parse", "--short", "job-1-branch"))
        self.assertEqual(server.auto_preview_branch(self.root, "job-1"), "")
        self.publish("0000000")
        self.assertEqual(server.auto_preview_branch(self.root, "job-1"), "job-1-branch")

    def test_a_job_still_being_built_doesnt_publish(self):
        server.write_settings(self.root, {"auto_preview": True})
        path = self.root / ".orchestrator" / "jobs" / "job-1.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "status": "executing"}))
        self.assertEqual(server.auto_preview_branch(self.root, "job-1"), "")

    def test_the_job_page_gets_the_published_link_and_a_running_live_preview(self):
        self.publish("0000000")
        live = self.root / ".orchestrator" / "output" / "dev-previews"
        live.mkdir(parents=True)
        (live / "job-1-branch.json").write_text(json.dumps({"branch": "job-1-branch", "url": "http://localhost:5173/", "pid": os.getpid()}))
        (live / "gone.json").write_text(json.dumps({"branch": "other", "url": "http://localhost:3000/", "pid": 999999}))
        p = server.job_preview(self.root, {"branch": "job-1-branch"})
        self.assertEqual(p["hosted"]["url"], "https://demo--job-1-branch-x.web.app")
        self.assertFalse(p["hosted_current"])
        self.assertEqual(p["live"]["url"], "http://localhost:5173/")
        self.assertEqual([x["branch"] for x in server.live_previews(self.root)], ["job-1-branch"])  # a stopped server isn't listed
        self.assertEqual(p["gaps"], [])

    def test_only_web_projects_get_a_preview(self):
        (self.root / ".orchestrator").mkdir(exist_ok=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_kind": "mobile", "mobile_app": True}))
        with mock.patch.object(server, "is_mobile_app", lambda root, config: True):
            self.assertIsNone(server.job_preview(self.root, {"branch": "job-1-branch"}))


class AutoPreviewWatchTests(unittest.TestCase):
    def test_when_the_jobs_run_ends_ready_for_review_its_link_is_published(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(server.shutil, "which", lambda name: f"/usr/bin/{name}"):
            root = web_project(tmp)
            server.write_settings(root, {"auto_preview": True})
            session = mock.Mock(running=False)
            handler = mock.Mock()
            launched = []
            handler._launch_run = lambda key, params, r: launched.append((key, params["branch"], params["job"]))
            with mock.patch.object(server.threading, "Thread", lambda target, **kw: mock.Mock(start=target)):
                server.UIHandler._preview_when_ready(handler, session, root, "job-1")
            self.assertEqual(launched, [("web_preview", "job-1-branch", "job-1")])
            handler._audit.assert_not_called()


class PreviewWiringTests(unittest.TestCase):
    def test_previews_are_errands_that_dont_make_the_job_busy(self):
        self.assertIn("web_preview", server.SIDE_ACTIONS)
        self.assertIn("dev_preview", server.SIDE_ACTIONS)
        self.assertEqual(server.AUTO_PREVIEW_AFTER, {"execute", "resume", "debug"})
        self.assertIn("dev_preview.py", " ".join(server.build_dev_preview({"branch": "feature/x"}, Path("/tmp"))))
        with self.assertRaises(server.UIError):
            server.build_dev_preview({"branch": "--help"}, Path("/tmp"))

    def test_the_app_may_frame_hosted_and_local_previews(self):
        source = (ROOT / "orchestrator" / "web" / "server.py").read_text()
        csp = source[source.index('"frame-src'):source.index("frame-ancestors")]
        for origin in ("https://*.web.app", "https://*.firebaseapp.com", "http://localhost:*", "http://127.0.0.1:*"):
            self.assertIn(origin, csp)

    def test_the_job_page_shows_the_preview_with_a_way_to_point_out_problems(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn("${jobPreviewCard(data.preview, runs)}", app)
        for text in ("Start live preview", "Stop live preview", "Point out a problem", "What's wrong here?",
                     "Publish a link when a job is ready for review", 'action: "dev_preview"'):
            self.assertIn(text, app)
        self.assertIn('runAction("debug", { job: id, feedback }', app)  # an idle job gets a fix run with the pin's note
        self.assertIn("const SIDE_RUNS = new Set([\"export_job\", \"web_preview\", \"dev_preview\"]);", app)


if __name__ == "__main__":
    unittest.main()
