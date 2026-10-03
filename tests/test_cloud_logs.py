from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import cloud_logs  # noqa: E402
from cloud_logs import CloudLogsError, RemoteLogsConfig, SentryLogsClient  # noqa: E402

CONFIG = RemoteLogsConfig(api_base="https://us.sentry.io", org="123", project="456")


class FakeResponse:
    def __init__(self, payload, link=None):
        self._body = json.dumps(payload).encode("utf-8")
        self.headers = {"Link": link} if link else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSentry:
    """Stands in for urlopen: returns queued responses and records each request's params."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[dict[str, list[str]]] = []

    def __call__(self, request, timeout=None):
        self.requests.append(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def http_error(code, detail):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(json.dumps({"detail": detail}).encode()))


def next_link(cursor):
    return (f'<https://x>; rel="previous"; results="false"; cursor="0:0:1", '
            f'<https://x>; rel="next"; results="true"; cursor="{cursor}"')


class ParsingTests(unittest.TestCase):
    def test_parse_dsn_saas_region(self):
        coords = cloud_logs.parse_dsn("https://abc123@o4510342215434240.ingest.us.sentry.io/4510342216941568")
        self.assertEqual(coords, {"api_base": "https://us.sentry.io", "org": "4510342215434240",
                                  "project": "4510342216941568"})

    def test_parse_dsn_saas_default_region(self):
        self.assertEqual(cloud_logs.parse_dsn("https://k@o1.ingest.sentry.io/2")["api_base"], "https://sentry.io")

    def test_parse_dsn_self_hosted_has_no_org(self):
        coords = cloud_logs.parse_dsn("https://k@sentry.example.com:9000/7")
        self.assertEqual(coords, {"api_base": "https://sentry.example.com:9000", "org": None, "project": "7"})

    def test_detect_dsn_skips_vendored_and_test_dirs(self):
        dsn = "https://c5e52d7fe6dd840927abcb9e8253a4e2@o1.ingest.us.sentry.io/2"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".swiftpm").mkdir()
            (root / ".swiftpm" / "Vendor.swift").write_text('let d = "https://ffffffffffffffff@o9.ingest.sentry.io/9"')
            (root / "AppTests").mkdir()
            (root / "AppTests" / "ComplianceTests.swift").write_text(
                'let fake = "https://abcdefabcdefabcdef@o1.ingest.sentry.io/123"')
            (root / "App").mkdir()
            (root / "App" / "App.swift").write_text(f'options.dsn = "{dsn}"')
            found = cloud_logs.detect_dsn(root)
        self.assertEqual(found[0], dsn)
        self.assertEqual(found[1].name, "App.swift")

    def test_parse_next_cursor(self):
        self.assertEqual(cloud_logs.parse_next_cursor(next_link("0:100:0")), "0:100:0")
        self.assertIsNone(cloud_logs.parse_next_cursor(next_link("x").replace('results="true"', 'results="false"')))
        self.assertIsNone(cloud_logs.parse_next_cursor(None))

    def test_level_query(self):
        self.assertIsNone(cloud_logs.level_query(None))
        self.assertIsNone(cloud_logs.level_query("trace"))
        self.assertEqual(cloud_logs.level_query("warning"), "severity:[warn,error,fatal]")
        with self.assertRaises(CloudLogsError):
            cloud_logs.level_query("loud")

    def test_since_validation(self):
        self.assertEqual(cloud_logs.since_to_stats_period("6H"), "6h")
        with self.assertRaises(CloudLogsError):
            cloud_logs.since_to_stats_period("yesterday")

    def test_format_log_line(self):
        line = cloud_logs.format_log_line({"timestamp": "2026-09-22T14:03:11.123456+00:00", "severity": "debug",
                                           "category": "LobbyViewModel", "message": "joined"})
        self.assertEqual(line, "2026-09-22 14:03:11.123Z  DEBUG  [LobbyViewModel] joined")

    def test_parse_session_start(self):
        info = cloud_logs.parse_session_start({
            "timestamp": "2026-09-22T14:00:00+00:00",
            "message": "remote_log.session_start session=1a2b3c4d channel=adhoc version=2.3 build=451",
            "user.id": "G:1", "device.model": "iPhone17,1",
        }, "app_session")
        self.assertEqual((info.session, info.channel, info.version, info.build, info.user, info.device),
                         ("1a2b3c4d", "adhoc", "2.3", "451", "G:1", "iPhone17,1"))
        self.assertIsNone(cloud_logs.parse_session_start({"message": "unrelated"}, "app_session"))


class ClientTests(unittest.TestCase):
    def test_query_follows_pagination_and_caps_rows(self):
        fake = FakeSentry(FakeResponse({"data": [{"message": "a"}, {"message": "b"}]}, next_link("c1")),
                          FakeResponse({"data": [{"message": "c"}]}, next_link("c2")))
        rows = SentryLogsClient(CONFIG, "t", urlopen=fake).query(["message"], "x:y", max_rows=3)
        self.assertEqual([r["message"] for r in rows], ["a", "b", "c"])
        self.assertEqual(fake.requests[0]["dataset"], ["logs"])
        self.assertEqual(fake.requests[0]["project"], ["456"])
        self.assertEqual(fake.requests[0]["query"], ["x:y"])
        self.assertEqual(fake.requests[1]["cursor"], ["c1"])
        self.assertEqual(fake.requests[1]["per_page"], ["1"])

    def test_query_retries_with_fallback_fields_on_400(self):
        fake = FakeSentry(http_error(400, "Unknown field: category"), FakeResponse({"data": [{"message": "a"}]}))
        rows = SentryLogsClient(CONFIG, "t", urlopen=fake).query(["message", "category"], fallback_fields=["message"])
        self.assertEqual(rows, [{"message": "a"}])
        self.assertEqual(fake.requests[1]["field"], ["message"])

    def test_forbidden_explains_required_scopes(self):
        fake = FakeSentry(http_error(403, "You do not have permission to perform this action."))
        with self.assertRaises(CloudLogsError) as ctx:
            SentryLogsClient(CONFIG, "t", urlopen=fake).query(["message"])
        self.assertIn("event:read", str(ctx.exception))
        self.assertIn("https://us.sentry.io/settings/account/api/auth-tokens/", str(ctx.exception))


class SessionTests(unittest.TestCase):
    def test_list_sessions_from_start_markers(self):
        fake = FakeSentry(FakeResponse({"data": [
            {"timestamp": "2026-09-22T14:00:00+00:00",
             "message": "remote_log.session_start session=bbbb channel=adhoc version=1 build=2"},
        ]}))
        sessions = cloud_logs.list_sessions(SentryLogsClient(CONFIG, "t", urlopen=fake))
        self.assertEqual([s.session for s in sessions], ["bbbb"])
        self.assertIn("remote_log.session_start", fake.requests[0]["query"][0])

    def test_list_sessions_falls_back_to_grouping_by_attribute(self):
        fake = FakeSentry(FakeResponse({"data": []}), FakeResponse({"data": [
            {"timestamp": "t3", "app_session": "new"}, {"timestamp": "t2", "app_session": "new"},
            {"timestamp": "t1", "app_session": "old"},
        ]}))
        sessions = cloud_logs.list_sessions(SentryLogsClient(CONFIG, "t", urlopen=fake))
        self.assertEqual([s.session for s in sessions], ["new", "old"])

    def test_fetch_session_logs_filters_and_returns_reading_order(self):
        fake = FakeSentry(FakeResponse({"data": [{"message": "second"}, {"message": "first"}]}))
        rows = cloud_logs.fetch_session_logs(SentryLogsClient(CONFIG, "t", urlopen=fake), "abcd",
                                             min_level="error", extra_query="category:Lobby")
        self.assertEqual([r["message"] for r in rows], ["first", "second"])
        self.assertEqual(fake.requests[0]["query"], ["app_session:abcd severity:[error,fatal] category:Lobby"])
        self.assertEqual(fake.requests[0]["sort"], ["-timestamp"])

    def test_write_pull_creates_log_meta_and_latest_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = cloud_logs.write_pull("abcd", [{"timestamp": "2026-09-22T14:00:00+00:00", "severity": "info",
                                                  "message": "hello"}], {"query": ""}, base_dir=Path(tmp))
            self.assertIn("INFO   hello", (out / "cloud.log").read_text())
            self.assertEqual(json.loads((out / "meta.json").read_text())["rows"], 1)
            self.assertEqual((Path(tmp) / "latest").resolve(), out.resolve())

    def test_tail_prints_only_new_lines(self):
        first = {"timestamp": "t1", "severity": "info", "message": "one"}
        second = {"timestamp": "t2", "severity": "info", "message": "two"}
        fake = FakeSentry(FakeResponse({"data": [first]}), FakeResponse({"data": [second, first]}))
        out = io.StringIO()
        cloud_logs.tail(SentryLogsClient(CONFIG, "t", urlopen=fake), "abcd", out=out, iterations=2,
                        sleep=lambda _: None)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].endswith("one"))
        self.assertTrue(lines[2].endswith("two"))

    def test_log_refs(self):
        self.assertTrue(cloud_logs.is_log_ref("cloud:latest"))
        self.assertFalse(cloud_logs.is_log_ref(".orchestrator/output/manual/x"))


class SetupTests(unittest.TestCase):
    def test_config_requires_org_and_api_base(self):
        with self.assertRaises(CloudLogsError):
            RemoteLogsConfig.from_dict({})
        with self.assertRaises(CloudLogsError):
            RemoteLogsConfig.from_dict({"api_base": "https://sentry.io"})

    def test_save_config_preserves_other_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.json"
            path.write_text(json.dumps({"project_name": "App", "scheme": "App"}))
            cloud_logs.save_config(CONFIG, path)
            data = json.loads(path.read_text())
        self.assertEqual(data["project_name"], "App")
        self.assertEqual(data["remote_logs"]["org"], "123")
        self.assertEqual(data["remote_logs"]["token_env"], "SENTRY_LOGS_TOKEN")

    def test_save_token_replaces_existing_entry_and_restricts_permissions(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=False):
            env_file = Path(tmp) / ".env"
            env_file.write_text("OTHER=1\nexport SENTRY_LOGS_TOKEN=old\n")
            cloud_logs.save_token("SENTRY_LOGS_TOKEN", "new", env_file)
            self.assertEqual(env_file.read_text(), "OTHER=1\nSENTRY_LOGS_TOKEN=new\n")
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_token_prefers_dedicated_env_over_upload_token(self):
        with patch.dict("os.environ", {"SENTRY_LOGS_TOKEN": "read", "SENTRY_AUTH_TOKEN": "upload"}):
            self.assertEqual(CONFIG.token(), "read")


if __name__ == "__main__":
    unittest.main()
