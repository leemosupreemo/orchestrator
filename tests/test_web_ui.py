from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator.web import server as ui  # noqa: E402

UI_HEADERS = {"Content-Type": "application/json", "X-Orchestrator-UI": "1"}


def make_project(root: Path) -> None:
    runtime = root / ".orchestrator"
    (runtime / "jobs").mkdir(parents=True)
    (runtime / "project.json").write_text(json.dumps({"project_name": "Demo", "scheme": "Demo"}))
    (runtime / "jobs" / "20260922-bug-1.json").write_text(json.dumps({
        "title": "Lobby seat stays empty", "type": "bug", "status": "debugging", "branch": "ai/issue-1",
        "tasks": ["repro", "fix"], "completed_tasks": ["0"],
    }))
    out = runtime / "output" / "20260922-bug-1"
    out.mkdir(parents=True)
    (out / "brief.md").write_text("# Brief\n")


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "project"
        make_project(self.root)
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": str(base / "state")})
        self.env.start()
        self.server = ui.UIServer(("127.0.0.1", 0), self.root, token="test-token")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.sessions.stop_all()
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, auth=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if auth:
            hdrs["Authorization"] = "Bearer test-token"
        if host is not None:
            hdrs["Host"] = host
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=payload, headers=hdrs)
        res = conn.getresponse()
        raw = res.read()
        conn.close()
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = raw
        return res, data


class AuthTests(ServerTestCase):
    def test_api_requires_token(self):
        res, _ = self.request("GET", "/api/state", auth=False)
        self.assertEqual(res.status, 401)
        res, data = self.request("GET", "/api/state")
        self.assertEqual(res.status, 200)
        self.assertEqual(data["project"]["name"], "Demo")

    def test_url_token_is_exchanged_for_http_only_cookie(self):
        res, _ = self.request("GET", "/?token=test-token", auth=False)
        self.assertEqual(res.status, 303)
        cookie = res.getheader("Set-Cookie")
        self.assertIn("orchestrator_ui=test-token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        res, _ = self.request("GET", "/api/jobs", auth=False, headers={"Cookie": "orchestrator_ui=test-token"})
        self.assertEqual(res.status, 200)

    def test_wrong_url_token_rejected(self):
        res, _ = self.request("GET", "/?token=nope", auth=False)
        self.assertEqual(res.status, 401)

    def test_foreign_host_header_rejected(self):
        res, _ = self.request("GET", "/api/state", host="evil.example:80")
        self.assertEqual(res.status, 403)

    def test_post_requires_ui_header(self):
        res, _ = self.request("POST", "/api/runs", body={"action": "check"},
                              headers={"Content-Type": "application/json"})
        self.assertEqual(res.status, 403)

    def test_static_index_served_with_csp(self):
        res, body = self.request("GET", "/", auth=False)
        self.assertEqual(res.status, 200)
        self.assertIn(b"Orchestrator", body)
        self.assertIn("frame-ancestors 'none'", res.getheader("Content-Security-Policy"))

    def test_static_traversal_blocked(self):
        res, _ = self.request("GET", "/../server.py", auth=False)
        self.assertEqual(res.status, 404)


class ReadApiTests(ServerTestCase):
    def test_jobs_list_and_detail(self):
        _, data = self.request("GET", "/api/jobs")
        job = data["jobs"][0]
        self.assertEqual((job["id"], job["status"], job["tasks_total"], job["tasks_done"]),
                         ("20260922-bug-1", "debugging", 2, 1))
        res, detail = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual(res.status, 200)
        self.assertEqual(detail["outputs"][0]["path"], "output/20260922-bug-1/brief.md")

    def test_job_id_validated(self):
        res, _ = self.request("GET", "/api/jobs/..%2Fproject")
        self.assertEqual(res.status, 400)
        res, _ = self.request("GET", "/api/jobs/missing")
        self.assertEqual(res.status, 404)

    def test_file_reads_are_confined_to_runtime_dir(self):
        res, data = self.request("GET", "/api/file?path=output/20260922-bug-1/brief.md")
        self.assertEqual((res.status, data["text"]), (200, "# Brief\n"))
        res, _ = self.request("GET", "/api/file?path=../../etc/passwd")
        self.assertEqual(res.status, 404)

    def test_devlogs_reports_unconfigured(self):
        _, data = self.request("GET", "/api/devlogs")
        self.assertEqual(data["sessions"]["configured"], False)
        self.assertEqual(data["pulls"], [])

    def test_switch_project_rejects_unknown_dirs(self):
        res, _ = self.request("POST", "/api/project", body={"root": "/tmp"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_job_writes_spec_file_and_passes_flags(self):
        argv = ui.build_new_job({"type": "feature", "summary": "Add rematch", "spec": "Details",
                                 "branch_mode": "new", "no_dispatch": True}, self.root)
        self.assertEqual(argv[3:8], ["script", "new_job.py", "feature", "--summary", "Add rematch"])
        spec = Path(argv[argv.index("--spec-file") + 1])
        self.assertEqual(spec.read_text(), "Details\n")
        self.assertIn(self.root / ".orchestrator" / "ui" / "specs", spec.parents)
        self.assertIn("--no-dispatch", argv)
        self.assertEqual(argv[argv.index("--branch-mode") + 1], "new")

    def test_new_job_validates_input(self):
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "rm -rf", "summary": "x"}, self.root)
        with self.assertRaises(ui.UIError):
            ui.build_new_job({"type": "bug", "summary": ""}, self.root)

    def test_job_actions_resolve_job_file(self):
        argv = ui.ACTIONS["debug"].build({"job": "20260922-bug-1", "logs": "cloud:latest", "feedback": "still broken"}, self.root)
        self.assertTrue(argv[5].endswith(".orchestrator/jobs/20260922-bug-1.json"))
        self.assertEqual(argv[-4:], ["--logs", "cloud:latest", "--feedback", "still broken"])
        with self.assertRaises(ui.UIError):
            ui.ACTIONS["execute"].build({"job": "../../x"}, self.root)

    def test_logs_pull_session_validated(self):
        self.assertEqual(ui.build_logs_pull({}, self.root)[-2:], ["pull", "--latest"])
        self.assertEqual(ui.build_logs_pull({"session": "1a2b3c4d", "level": "warn"}, self.root)[-4:],
                         ["--session", "1a2b3c4d", "--level", "warn"])
        with self.assertRaises(ui.UIError):
            ui.build_logs_pull({"session": "x; rm -rf /"}, self.root)

    def test_every_action_uses_the_cli_entry_point(self):
        for key, action in ui.ACTIONS.items():
            params = {"job": "20260922-bug-1", "summary": "s", "feedback": "f", "answer": "a"}
            argv = action.build(params, self.root)
            self.assertEqual(argv[:3], [sys.executable, "-m", "orchestrator"], key)


class JobStateTests(unittest.TestCase):
    def state(self, **job):
        return ui.job_state(job)

    def test_each_status_has_one_next_action(self):
        cases = {
            "planned": ("needs_you", "attention", "schedule"),
            "scheduled": ("working", "working", "execute"),
            "executing": ("working", "working", None),
            "completed": ("done", "done", None),
        }
        for status, (group, tone, action) in cases.items():
            st = self.state(status=status)
            self.assertEqual((st["group"], st["tone"], st["next"] and st["next"]["action"]), (group, tone, action), status)

    def test_review_needed_offers_merge_only_with_a_pr(self):
        self.assertEqual(self.state(status="review-needed", pr_number=141)["next"]["action"], "merge")
        self.assertEqual(self.state(status="review-needed")["next"]["action"], "console")

    def test_debugging_is_red_only_when_tests_fail(self):
        failing = self.state(status="debugging", test_status="tests-failed")
        self.assertEqual((failing["tone"], failing["label"], failing["next"]["action"]), ("failed", "Tests failing", "debug"))
        self.assertEqual(self.state(status="debugging")["tone"], "attention")

    def test_question_needs_an_answer(self):
        st = self.state(status="human-needed", human_clarification_question="Which lobby size?")
        self.assertEqual((st["group"], st["next"]["action"]), ("needs_you", "answer"))

    def test_kind_labels_are_plain_language(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "j.json"
            path.write_text("{}")
            self.assertEqual(ui.job_summary(path, {"type": "bug-fix"})["kind"], "Bug fix")
            self.assertEqual(ui.job_summary(path, {"type": "test-audit"})["kind"], "Tests")


class LinkedLogTests(unittest.TestCase):
    def test_directories_expand_to_log_files_and_cloud_refs_are_described(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_project(root)
            pull = root / ".orchestrator" / "output" / "cloud_logs" / "20260921-1a2b"
            pull.mkdir(parents=True)
            (pull / "cloud.log").write_text("x")
            entry = ui.resolve_linked_log(root, ".orchestrator/output/cloud_logs/20260921-1a2b")
            self.assertEqual(entry["files"], ["output/cloud_logs/20260921-1a2b/cloud.log"])
            self.assertEqual(ui.resolve_linked_log(root, "cloud:latest")["files"], [])
            self.assertIn("Newest device launch", ui.resolve_linked_log(root, "cloud:latest")["label"])
            self.assertEqual(ui.resolve_linked_log(root, "/etc")["files"], [])


class PtySessionTests(unittest.TestCase):
    def collect(self, session, until, timeout=10):
        offset, out = 0, b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            offset, data, finished = session.read(offset, timeout=0.5)
            out += data
            if until(out) or finished:
                return out, finished
        return out, False

    def test_runs_in_a_real_tty_and_takes_input(self):
        script = "import os; print('tty', os.isatty(0)); print('got', input('name? '))"
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t1", "test", "Test", [sys.executable, "-c", script], Path(tmp), dict(os.environ))
            out, _ = self.collect(session, lambda o: b"name?" in o)
            self.assertIn(b"tty True", out)
            session.write(b"alice\r")
            out, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertIn(b"got alice", out)
            self.assertEqual(session.exit_code, 0)

    def test_has_controlling_terminal_for_getpass(self):
        script = "import os; fd = os.open('/dev/tty', os.O_RDWR); os.write(fd, b'ctty ok\\n')"
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t2", "test", "Test", [sys.executable, "-c", script], Path(tmp), dict(os.environ))
            out, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertIn(b"ctty ok", out)

    def test_stop_terminates_process_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t3", "test", "Test", [sys.executable, "-c", "import time; time.sleep(60)"],
                                    Path(tmp), dict(os.environ))
            session.stop()
            _, finished = self.collect(session, lambda o: False)
            self.assertTrue(finished)
            self.assertNotEqual(session.exit_code, 0)

    def test_late_reader_catches_up_from_offset_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = ui.PtySession("t4", "test", "Test", [sys.executable, "-c", "print('early')"], Path(tmp), dict(os.environ))
            self.collect(session, lambda o: False)
            _, data, finished = session.read(0, timeout=0)
            self.assertIn(b"early", data)
            self.assertTrue(finished)


class RunApiTests(ServerTestCase):
    def test_start_stream_and_input(self):
        echo = ui.Action("Echo", lambda p, r: [sys.executable, "-c", "print('ready'); print('you said', input())"])
        with patch.dict(ui.ACTIONS, {"echo": echo}):
            res, data = self.request("POST", "/api/runs", body={"action": "echo"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 201)
        sid = data["run"]["id"]
        session = self.server.sessions.get(sid)
        deadline = time.time() + 10
        while b"ready" not in session.read(0, 0.2)[1] and time.time() < deadline:
            pass
        res, _ = self.request("POST", f"/api/runs/{sid}/input", body={"data": "hi\r"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", f"/api/runs/{sid}/stream?offset=0", headers={"Authorization": "Bearer test-token"})
        stream = conn.getresponse()
        self.assertEqual(stream.getheader("Content-Type"), "text/event-stream")
        text = stream.read().decode()
        conn.close()
        chunks = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ") and '"data"' in line]
        output = b"".join(base64.b64decode(c["data"]) for c in chunks)
        self.assertIn(b"you said hi", output)
        self.assertIn("event: end", text)
        transcript = list((self.root / ".orchestrator" / "logs" / "ui").glob(f"{sid}-echo.log"))
        self.assertEqual(len(transcript), 1)

    def test_run_starts_at_requested_terminal_size(self):
        probe = ui.Action("Size", lambda p, r: [sys.executable, "-c",
                                                "import os; s = os.get_terminal_size(); print('size', s.columns, s.lines)"])
        with patch.dict(ui.ACTIONS, {"size": probe}):
            _, data = self.request("POST", "/api/runs", body={"action": "size", "cols": 48, "rows": 20}, headers=UI_HEADERS)
        session = self.server.sessions.get(data["run"]["id"])
        out, deadline = b"", time.time() + 10
        while b"size" not in out and time.time() < deadline:
            out = session.read(0, 0.2)[1]
        self.assertIn(b"size 48 20", out)

    def wait_finished(self, sid, timeout=10):
        session = self.server.sessions.get(sid)
        deadline = time.time() + timeout
        while session.running and time.time() < deadline:
            time.sleep(0.05)
        return session

    def test_job_runs_are_titled_and_linked_to_their_job(self):
        noop = ui.Action("Run fix", lambda p, r: [sys.executable, "-c", "pass"], fields=["job"])
        with patch.dict(ui.ACTIONS, {"noop": noop}):
            _, data = self.request("POST", "/api/runs", body={"action": "noop", "params": {"job": "20260922-bug-1"}}, headers=UI_HEADERS)
        self.assertEqual(data["run"]["title"], "Run fix · Lobby seat stays empty")
        self.assertEqual(data["run"]["job"], "20260922-bug-1")
        _, detail = self.request("GET", "/api/jobs/20260922-bug-1")
        self.assertEqual([r["id"] for r in detail["runs"]], [data["run"]["id"]])

    def test_new_job_run_reports_the_job_it_created(self):
        jobs = self.root / ".orchestrator" / "jobs"
        script = f"import json,time; time.sleep(0.3); open({str(jobs / 'new-1.json')!r},'w').write(json.dumps({{'title':'x'}}))"
        fake = ui.Action("New job", lambda p, r: [sys.executable, "-c", script])
        with patch.dict(ui.ACTIONS, {"new_job": fake}):
            _, data = self.request("POST", "/api/runs", body={"action": "new_job"}, headers=UI_HEADERS)
        self.wait_finished(data["run"]["id"])
        time.sleep(1.2)
        _, runs = self.request("GET", "/api/runs")
        self.assertEqual(runs["runs"][0]["result_job"], "new-1")

    def test_jobs_list_marks_jobs_with_a_running_run(self):
        sleeper = ui.Action("Run fix", lambda p, r: [sys.executable, "-c", "import time; time.sleep(30)"], fields=["job"])
        with patch.dict(ui.ACTIONS, {"sleep": sleeper}):
            self.request("POST", "/api/runs", body={"action": "sleep", "params": {"job": "20260922-bug-1"}}, headers=UI_HEADERS)
        _, data = self.request("GET", "/api/jobs")
        self.assertTrue(data["jobs"][0]["active_run"])

    def test_unknown_action_rejected(self):
        res, data = self.request("POST", "/api/runs", body={"action": "shell", "params": {"cmd": "ls"}}, headers=UI_HEADERS)
        self.assertEqual(res.status, 400)
        self.assertIn("Unknown action", data["error"])

    def test_unknown_run_is_404(self):
        res, _ = self.request("GET", "/api/runs/nope/stream")
        self.assertEqual(res.status, 404)


if __name__ == "__main__":
    unittest.main()
