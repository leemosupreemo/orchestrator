"""Web project previews: the local live preview's dev server, and the job page's preview."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
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

    def test_a_site_without_a_build_is_served_from_the_folder_with_its_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "public").mkdir()
            (folder / "public" / "index.html").write_text("<h1>Hi</h1>")
            setup, server = dev_preview.dev_commands(folder, {})
            self.assertEqual(setup, [])
            self.assertIn("http.server", server)
            self.assertEqual(server[server.index("--directory") + 1], str(folder / "public"))

    def test_nothing_to_run_says_what_to_do_instead_of_listing_the_projects_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.py").write_text("print('hi')")
            with self.assertRaisesRegex(ValueError, "already running"):
                dev_preview.dev_commands(Path(tmp), {})

    def test_a_django_app_runs_its_own_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manage.py").write_text("")
            server = dev_preview.dev_commands(Path(tmp), {})[1]
            self.assertEqual(server[2:4], ["manage.py", "runserver"])
            self.assertRegex(server[4], r"^127\.0\.0\.1:\d+$")

    def test_orchestrators_own_code_runs_orchestrator_with_a_project_and_state_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "web-ui"
            (folder / "orchestrator" / "web").mkdir(parents=True)
            (folder / "orchestrator" / "web" / "server.py").write_text("")
            (folder / "orchestrator" / "__main__.py").write_text("")
            with patch.dict("os.environ", {"ORCHESTRATOR_USER_STATE_DIR": str(Path(tmp) / "state")}):
                (Path(tmp) / "state").mkdir()
                (Path(tmp) / "state" / "ui_token").write_text("tok123")
                server = dev_preview.dev_commands(folder, {})[1]
                env = dev_preview.dev_env(folder)
            self.assertEqual(server[server.index("-m") + 1:server.index("-m") + 3], ["orchestrator", "ui"])
            project = Path(server[server.index("--project") + 1])
            self.assertTrue((project / ".orchestrator" / "project.json").is_file())  # a sample project, never a real one
            self.assertNotIn(folder, project.parents)
            self.assertEqual(server[server.index("--token") + 1], "tok123")  # the same sign-in cookie works for both
            self.assertEqual(env["PYTHONPATH"], str(folder))  # the branch's own code
            self.assertNotEqual(env["ORCHESTRATOR_USER_STATE_DIR"], str(Path(tmp) / "state"))

    def test_an_app_already_running_is_shown_instead_of_starting_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(dev_preview, "start_proxy", return_value="http://localhost:9/"), \
                 patch.object(dev_preview.threading.Event, "wait", return_value=True):
                self.assertEqual(dev_preview.show_running_app(root, "main", "main", "https://example.com"), 1)  # not this computer
                self.assertEqual(dev_preview.show_running_app(root, "main", "main", "http://localhost:3000"), 0)

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

    def test_home_gets_the_published_link_and_a_running_live_preview_of_the_current_build(self):
        subprocess.run(["git", "checkout", "-q", "job-1-branch"], cwd=self.root, check=True)
        self.publish("0000000")
        live = self.root / ".orchestrator" / "output" / "dev-previews"
        live.mkdir(parents=True)
        (live / "job-1-branch.json").write_text(json.dumps({"branch": "job-1-branch", "url": "http://localhost:5173/", "pid": os.getpid()}))
        (live / "gone.json").write_text(json.dumps({"branch": "other", "url": "http://localhost:3000/", "pid": 999999}))
        p = server.project_preview(self.root)
        self.assertEqual(p["branch"], "job-1-branch")  # whatever is checked out in the project folder
        self.assertEqual(p["hosted"]["url"], "https://demo--job-1-branch-x.web.app")
        self.assertFalse(p["hosted_current"])
        self.assertEqual(p["live"]["url"], "http://localhost:5173/")
        self.assertEqual([x["branch"] for x in server.live_previews(self.root)], ["job-1-branch"])  # a stopped server isn't listed
        self.assertEqual(p["gaps"], [])
        self.assertTrue(p["branch_exists"])

    def test_the_current_build_is_the_checked_out_branch(self):
        self.assertEqual(server.project_preview(self.root)["branch"], "main")

    def test_only_web_projects_get_a_preview(self):
        (self.root / ".orchestrator").mkdir(exist_ok=True)
        (self.root / ".orchestrator" / "project.json").write_text(json.dumps({"project_kind": "mobile", "mobile_app": True}))
        with mock.patch.object(server, "is_mobile_app", lambda root, config: True):
            self.assertIsNone(server.project_preview(self.root))


class PreviewProxyTests(unittest.TestCase):
    """The live preview goes through a proxy that adds the element-picking helper to pages and passes the rest through."""

    def setUp(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class DevServer(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Upgrade") == "websocket":  # a live-reload socket: answer, then echo what comes
                    self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
                    self.wfile.flush()
                    data = self.connection.recv(100)
                    self.connection.sendall(b"echo:" + data)
                    return
                if self.path == "/host":  # what the app was reached as
                    body = (self.headers.get("Host") or "").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                html = self.path == "/"
                body = b"<html><head><title>t</title></head><body>hi</body></html>" if html else b"console.log(1)"
                self.send_response(200)
                self.send_header("Content-Type", "text/html" if html else "text/javascript")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), DevServer)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.proxy = dev_preview.start_proxy(f"http://localhost:{self.upstream.server_address[1]}/")
        self.port = int(self.proxy.rsplit(":", 1)[1].strip("/"))
        self.socket = socket

    def tearDown(self):
        self.upstream.shutdown()

    def get(self, path):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as reply:
            return reply.read(), dict(reply.headers)

    def test_pages_get_the_helper_and_lose_a_policy_that_would_block_it(self):
        body, headers = self.get("/")
        self.assertIn(b'<head><script src="/__orchestrator/preview-helper.js"></script><title>', body)
        self.assertNotIn("Content-Security-Policy", headers)
        self.assertNotIn("X-Frame-Options", headers)  # it would leave the job page's frame blank
        helper, _ = self.get("/__orchestrator/preview-helper.js")
        self.assertIn(b"orchestrator-preview", helper)

    def test_everything_else_passes_through_unchanged(self):
        body, headers = self.get("/app.js")
        self.assertEqual(body, b"console.log(1)")
        self.assertEqual(headers.get("Content-Security-Policy"), "default-src 'none'")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")

    def test_live_reload_sockets_are_passed_through(self):
        conn = self.socket.create_connection(("127.0.0.1", self.port), timeout=5)
        conn.sendall(b"GET /hmr HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        reply = conn.recv(200)
        self.assertTrue(reply.startswith(b"HTTP/1.1 101"))
        conn.sendall(b"ping")
        echoed = b""
        while not echoed.endswith(b"ping"):
            echoed += conn.recv(100)
        self.assertIn(b"echo:ping", reply + echoed)
        conn.close()

    def test_the_app_sees_its_own_address_and_the_link_keeps_its_query(self):
        body, _ = self.get("/host")
        self.assertEqual(body.decode(), f"127.0.0.1:{self.upstream.server_address[1]}")  # apps that check their Host accept it
        shown = dev_preview.start_proxy(f"http://127.0.0.1:{self.upstream.server_address[1]}/?token=abc")
        self.assertTrue(shown.endswith("/?token=abc"))  # a sign-in link still signs in

    def test_a_page_without_a_head_still_gets_the_helper(self):
        self.assertTrue(dev_preview.add_helper(b"<p>x</p>").startswith(b"<script src="))


class PreviewShotTests(unittest.TestCase):
    def test_a_screenshot_is_saved_with_the_jobs_output(self):
        import base64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = server.save_preview_shot(root, "job-1", "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg").decode())
            self.assertTrue(path.startswith(".orchestrator/output/job-1/preview-feedback/") and path.endswith(".jpg"))
            self.assertEqual((root / path).read_bytes(), b"\xff\xd8jpeg")
            path = server.save_preview_shot(root, "", "data:image/png;base64," + base64.b64encode(b"png").decode())
            self.assertTrue(path.startswith(".orchestrator/output/preview-feedback/") and path.endswith(".png"))  # from Home
            for bad in ("data:text/html;base64,PGI+", "https://example.com/x.png", "data:image/png;base64,***"):
                with self.assertRaises(server.UIError):
                    server.save_preview_shot(root, "job-1", bad)


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

    def test_home_shows_the_preview_with_a_way_to_point_out_problems(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn("${previewCard(homePreview?.preview, state.runs)}", app)
        self.assertEqual(app.count("previewCard("), 2)  # defined once, shown only on Home: never on a job page
        for text in ("Start live preview", "Stop live preview", "Point out a problem",
                     "Publish a link when a job is ready for review", 'action: "dev_preview"'):
            self.assertIn(text, app)
        self.assertIn('action: "new_job", params: { type: "quick", summary, branch_mode: "current" }', app)  # a new job, made without leaving Home
        for text in ('[["desktop", "Desktop"], ["tablet", "Tablet"], ["phone", "Phone"]]', "Add to message",
                     "Hold ⌘ or Shift to pick more", '"previews/shot"', "Screenshot of them: "):
            self.assertIn(text, app)
        css = (ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()
        self.assertIn('.job-preview-frame[data-device="phone"] { max-width: 390px; }', css)
        self.assertIn("const SIDE_RUNS = new Set([\"export_job\", \"web_preview\", \"dev_preview\"]);", app)


    def test_errors_on_the_live_preview_reach_the_job_page_and_can_be_sent_as_a_fix(self):
        helper = (ROOT / "orchestrator" / "web" / "static" / "preview-helper.js").read_text()
        for text in ('addEventListener("error"', '"unhandledrejection"', "console.error", 'type: "problem"', 'type: "problems"'):
            self.assertIn(text, helper)
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ('type === "problem"', "Fix with AI", "errors on the live preview of", "job-preview-problems"):
            self.assertIn(text, app)

    def test_opening_home_starts_the_live_preview_once(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ("PREVIEW_AUTOSTARTED", "orchestrator.previewAutoStart", "Start the live preview when I open Home", "p.branch_exists"):
            self.assertIn(text, app)

    def test_the_live_preview_is_not_counted_as_work(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.assertIn('const isPreviewRun = (r) => r.action === "dev_preview";', app)
        badge = app[app.index('const badge = $("#running-badge");') - 300:app.index('const badge = $("#running-badge");')]
        self.assertIn("!isPreviewRun(r)", badge)  # the sidebar's running count
        activity = app[app.index("pages.activity = async"):app.index("pages.file = async")]
        self.assertEqual(activity.count("!isPreviewRun(r)"), 3)  # running, finished and earlier runs

    def test_what_you_point_at_is_attached_to_the_message_as_chips(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ('id="preview-chips"', "attached.push({", "What they pointed at:", "data-chip-remove"):
            self.assertIn(text, app)
        self.assertNotIn('formDialog("What should change?"', app)  # no dialog in the way

    def test_the_frame_says_what_state_the_live_preview_is_in(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ("Starting live preview…", "Live preview couldn't start", "Live preview stopped responding",
                     "The preview isn't running", 'data-preview-do="restart"', "PREVIEW_FAILED"):
            self.assertIn(text, app)

    def test_the_page_path_follows_the_app_and_can_be_typed(self):
        helper = (ROOT / "orchestrator" / "web" / "static" / "preview-helper.js").read_text()
        for text in ('type: "location"', '"pushState", "replaceState"', '"popstate"'):
            self.assertIn(text, helper)
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ('id="preview-path"', 'type === "location"', 'id="preview-refresh"', "e.shiftKey"):
            self.assertIn(text, app)

    def test_a_change_can_be_described_under_the_preview(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        for text in ('id="job-preview-ask"', "Describe a change", "Shift+Enter"):
            self.assertIn(text, app)


if __name__ == "__main__":
    unittest.main()
