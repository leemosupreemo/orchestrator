from __future__ import annotations

import http.client
import io
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import connection_log  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402
from test_web_ui import UI_HEADERS, ServerTestCase  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


class ConnectionLogServerTests(ServerTestCase):
    def test_api_requests_are_logged_without_the_query(self):
        self.request("GET", "/api/state?token=test-token", auth=False)
        self.request("GET", "/api/state", auth=False)
        self.request("GET", "/app.js")  # pages and scripts aren't API requests
        log = connection_log.logs_dir() / "requests.jsonl"
        entries = read_jsonl(log)
        self.assertEqual([(e["path"], e["status"]) for e in entries], [("/api/state", "200"), ("/api/state", "401")])
        self.assertTrue(all(e["ms"].isdigit() for e in entries))
        self.assertNotIn("test-token", log.read_text())

    def test_page_reports_are_kept_with_known_fields_only(self):
        events = [{"kind": "fetch_failed", "path": "state", "ms": 30012, "error": "TypeError: Failed to fetch",
                   "online": True, "secret": "drop me"}, "not an event"]
        res, data = self.request("POST", "/api/client-log", body={"events": events}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200, data)
        self.assertEqual(data["kept"], 1)
        [entry] = read_jsonl(connection_log.logs_dir() / "client.jsonl")
        self.assertEqual((entry["event"], entry["path"], entry["ms"], entry["online"]), ("fetch_failed", "state", "30012", "True"))
        self.assertNotIn("secret", entry)

    def test_page_reports_need_a_sign_in_and_the_page_header(self):
        res, _ = self.request("POST", "/api/client-log", body={"events": [{"kind": "x"}]}, headers=UI_HEADERS, auth=False)
        self.assertEqual(res.status, 401)
        res, _ = self.request("POST", "/api/client-log", body={"events": [{"kind": "x"}]})
        self.assertEqual(res.status, 403)
        self.assertEqual(read_jsonl(connection_log.logs_dir() / "client.jsonl"), [])


    def test_page_reports_validation_and_capping(self):
        # Non-list events
        res, data = self.request("POST", "/api/client-log", body={"events": "not-a-list"}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual(data["kept"], 0)

        # Capped at 100 events, non-dict skipped, missing kind defaults to 'event'
        events = [{"detail": "ignored"}] + [{"kind": f"ev-{i}", "path": "test"} for i in range(120)]
        res, data = self.request("POST", "/api/client-log", body={"events": events}, headers=UI_HEADERS)
        self.assertEqual(res.status, 200)
        self.assertEqual(data["kept"], 100)
        entries = read_jsonl(connection_log.logs_dir() / "client.jsonl")
        self.assertEqual(entries[0]["event"], "event")
        self.assertEqual(entries[1]["event"], "ev-0")

    def test_dropped_connections_are_logged_to_requests_log(self):
        handler = ui.UIHandler.__new__(ui.UIHandler)
        handler.server = self.server
        handler.path = "/api/state"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Host": f"127.0.0.1:{self.port}", "Authorization": "Bearer test-token"}
        handler._drain_body = lambda: None

        with patch.object(ui.UIHandler, "_api", side_effect=BrokenPipeError):
            handler.command = "GET"
            handler._dispatch("GET")
        with patch.object(ui.UIHandler, "_api", side_effect=ConnectionResetError):
            handler.command = "POST"
            handler.headers["X-Orchestrator-UI"] = "1"
            handler._dispatch("POST")

        entries = read_jsonl(connection_log.logs_dir() / "requests.jsonl")
        dropped = [e for e in entries if e.get("event") == "dropped"]
        self.assertEqual(len(dropped), 2)
        self.assertEqual([d["error"] for d in dropped], ["BrokenPipeError", "ConnectionResetError"])
        self.assertEqual([d["method"] for d in dropped], ["GET", "POST"])
        self.assertEqual([d["path"] for d in dropped], ["/api/state", "/api/state"])


class TunnelOutputTests(ServerTestCase):
    def test_cloudflared_output_is_kept(self):
        proc = SimpleNamespace(stdout=io.StringIO("2026-10-03T19:06:12Z INF Registered tunnel connection\n"))
        ui._drain(proc)
        path = connection_log.tunnel_log_path()
        for _ in range(50):
            if path.exists() and path.read_text():
                break
            time.sleep(0.02)
        self.assertIn("Registered tunnel connection", path.read_text())

    def test_copy_to_handles_none_and_exceptions(self):
        # sink is None
        ui._copy_to(None, "sample line\n")

        # sink.write raises OSError or ValueError
        mock_sink = MagicMock()
        mock_sink.write.side_effect = OSError("write error")
        ui._copy_to(mock_sink, "sample line\n")

        mock_sink.write.side_effect = ValueError("file closed")
        ui._copy_to(mock_sink, "sample line\n")

    def test_open_tunnel_log_permissions_and_rotation(self):
        path = connection_log.tunnel_log_path()
        handle = connection_log.open_tunnel_log()
        self.assertIsNotNone(handle)
        handle.write("tunnel line 1\n")
        handle.close()
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

        # When file size >= MAX_TUNNEL_BYTES, open_tunnel_log rotates to .1
        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = SimpleNamespace(st_size=connection_log.MAX_TUNNEL_BYTES + 10)
            handle2 = connection_log.open_tunnel_log()
            self.assertIsNotNone(handle2)
            handle2.close()
        self.assertTrue(path.with_name(path.name + ".1").exists())

        # When open raises OSError, returns None
        with patch.object(Path, "open", side_effect=OSError("permission denied")):
            self.assertIsNone(connection_log.open_tunnel_log())


class FormatTests(unittest.TestCase):
    def test_lines_read_as_one_stream(self):
        server = json.dumps({"at": 0, "event": "request", "who": "", "method": "GET", "path": "/api/state", "status": "200", "ms": "12", "ip": "1.2.3.4"})
        browser = json.dumps({"at": 0, "event": "fetch_failed", "who": "me", "path": "state", "error": "Load failed"})
        self.assertRegex(connection_log.format_line("server", server), r"^\d\d:\d\d:\d\d server   GET /api/state 200 12ms ip=1\.2\.3\.4$")
        self.assertRegex(connection_log.format_line("browser", browser), r"browser  fetch_failed path=state error=Load failed$")
        self.assertEqual(connection_log.format_line("tunnel", "INF hello\n"), "tunnel   INF hello")

    def test_format_line_edge_cases(self):
        # Invalid JSON
        self.assertEqual(connection_log.format_line("server", "not-valid-json"), "server   not-valid-json")
        self.assertEqual(connection_log.format_line("browser", "not-valid-json"), "browser  not-valid-json")

        # Non-dict JSON
        self.assertEqual(connection_log.format_line("server", "[1, 2, 3]"), "server   [1, 2, 3]")
        self.assertEqual(connection_log.format_line("browser", '"just-a-string"'), 'browser  "just-a-string"')

        # Missing at / at=0
        entry = json.dumps({"event": "custom", "key": "val"})
        self.assertRegex(connection_log.format_line("browser", entry), r"^\d\d:\d\d:\d\d browser  custom key=val$")

        # Tunnel source ignores JSON parsing
        self.assertEqual(connection_log.format_line("tunnel", '{"json": true}'), 'tunnel   {"json": true}')


class ShowAndTailTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ORCHESTRATOR_USER_STATE_DIR": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_size_and_tail_helpers(self):
        missing = Path(self.tmp.name) / "missing.txt"
        self.assertEqual(connection_log._size(missing), 0)
        self.assertEqual(connection_log._tail(missing, 10), [])

        test_file = Path(self.tmp.name) / "test.txt"
        test_file.write_text("line 1\nline 2\nline 3\n")
        self.assertEqual(connection_log._size(test_file), len("line 1\nline 2\nline 3\n"))
        self.assertEqual(connection_log._tail(test_file, 2), ["line 2", "line 3"])
        self.assertEqual(connection_log._tail(test_file, 10), ["line 1", "line 2", "line 3"])

        # Large file (> 200,000 bytes)
        large_file = Path(self.tmp.name) / "large.txt"
        large_file.write_bytes(b"x" * 250_000 + b"\nfinal line\n")
        self.assertEqual(connection_log._tail(large_file, 1), ["final line"])

    def test_show_without_follow(self):
        folder = connection_log.logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "requests.jsonl").write_text(json.dumps({"at": 0, "event": "request", "method": "GET", "path": "/api/state", "status": 200, "ms": 5}) + "\n")
        (folder / "cloudflared.log").write_text("tunnel up\n")

        buf = io.StringIO()
        res = connection_log.show(lines=10, follow=False, out=buf)
        self.assertEqual(res, 0)
        output = buf.getvalue()
        self.assertIn("Connection logs in ", output)
        self.assertIn("-- requests.jsonl (last 1)", output)
        self.assertIn("GET /api/state 200 5ms", output)
        self.assertIn("-- cloudflared.log (last 1)", output)
        self.assertIn("tunnel   tunnel up", output)

    def test_show_with_follow_and_rotation(self):
        folder = connection_log.logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tunnel = folder / "cloudflared.log"
        tunnel.write_text("initial tunnel log\n")

        buf = io.StringIO()
        step = 0

        def fake_sleep(_duration):
            nonlocal step
            step += 1
            if step == 1:
                # Append new line
                with tunnel.open("a") as f:
                    f.write("followed tunnel line\n")
            elif step == 2:
                # Simulate rotation (file shrinks)
                tunnel.write_text("rotated new line\n")
            else:
                raise KeyboardInterrupt

        with patch("time.sleep", side_effect=fake_sleep):
            res = connection_log.show(lines=10, follow=True, out=buf)
        self.assertEqual(res, 0)
        output = buf.getvalue()
        self.assertIn("-- following (Ctrl-C to stop)", output)
        self.assertIn("tunnel   followed tunnel line", output)
        self.assertIn("tunnel   rotated new line", output)


class CliConnectionLogTests(unittest.TestCase):
    def test_cli_dispatches_to_connection_log_show(self):
        from orchestrator import cli
        with patch("orchestrator.connection_log.show", return_value=0) as mock_show:
            code = cli.main(["connection-log"])
            self.assertEqual(code, 0)
            mock_show.assert_called_once_with(lines=40, follow=False)

        with patch("orchestrator.connection_log.show", return_value=0) as mock_show:
            code = cli.main(["connection-log", "-n", "10", "-f"])
            self.assertEqual(code, 0)
            mock_show.assert_called_once_with(lines=10, follow=True)


if __name__ == "__main__":
    unittest.main()
