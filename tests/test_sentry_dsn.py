from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator.sentry_dsn import detect_dsn, parse_dsn


class SentryDsnTests(unittest.TestCase):
    def test_parse_saas_dsn_with_region(self):
        dsn = "https://abcdef0123456789@o123456.ingest.us.sentry.io/789012"
        res = parse_dsn(dsn)
        self.assertEqual(res, {
            "api_base": "https://us.sentry.io",
            "org": "123456",
            "project": "789012",
        })

    def test_parse_saas_dsn_without_region(self):
        dsn = "https://abcdef0123456789@o987654.ingest.sentry.io/321"
        res = parse_dsn(dsn)
        self.assertEqual(res, {
            "api_base": "https://sentry.io",
            "org": "987654",
            "project": "321",
        })

    def test_parse_self_hosted_dsn_with_port(self):
        dsn = "https://abcdef0123456789@sentry.internal.net:8443/42"
        res = parse_dsn(dsn)
        self.assertEqual(res, {
            "api_base": "https://sentry.internal.net:8443",
            "org": None,
            "project": "42",
        })

    def test_parse_self_hosted_dsn_without_port(self):
        dsn = "https://abcdef0123456789@sentry.internal.net/100"
        res = parse_dsn(dsn)
        self.assertEqual(res, {
            "api_base": "https://sentry.internal.net",
            "org": None,
            "project": "100",
        })

    def test_parse_dsn_no_project(self):
        res = parse_dsn("https://abcdef0123456789@sentry.internal.net/")
        self.assertIsNone(res["project"])

    def test_detect_dsn_prefers_production_over_test_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_dir = root / "Tests"
            test_dir.mkdir()
            mock_file = test_dir / "MockTests.swift"
            mock_file.write_text('let dsn = "https://0123456789abcdef0123456789abcdef@o111.ingest.sentry.io/111"')

            app_dir = root / "Sources"
            app_dir.mkdir()
            app_file = app_dir / "App.swift"
            app_file.write_text('let dsn = "https://0123456789abcdef0123456789abcdef@o222.ingest.sentry.io/222"')

            found = detect_dsn(root)
            self.assertIsNotNone(found)
            dsn_val, path_val = found
            self.assertIn("o222", dsn_val)
            self.assertEqual(path_val, app_file)

    def test_detect_dsn_fallback_to_test_fixture_when_no_prod_dsn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            test_dir = root / "spec"
            test_dir.mkdir()
            fixture_file = test_dir / "fixture.swift"
            fixture_file.write_text('let dsn = "https://0123456789abcdef0123456789abcdef@o333.ingest.sentry.io/333"')

            found = detect_dsn(root)
            self.assertIsNotNone(found)
            dsn_val, path_val = found
            self.assertIn("o333", dsn_val)
            self.assertEqual(path_val, fixture_file)

    def test_detect_dsn_skips_excluded_dirs_and_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            node_dir = root / "node_modules"
            node_dir.mkdir()
            (node_dir / "index.js").write_text('https://0123456789abcdef0123456789abcdef@o444.ingest.sentry.io/444')

            hidden_dir = root / ".hidden"
            hidden_dir.mkdir()
            (hidden_dir / "conf.json").write_text('https://0123456789abcdef0123456789abcdef@o555.ingest.sentry.io/555')

            asset_dir = root / "Media.xcassets"
            asset_dir.mkdir()
            (asset_dir / "asset.json").write_text('https://0123456789abcdef0123456789abcdef@o666.ingest.sentry.io/666')

            unsupported = root / "readme.txt"
            unsupported.write_text('https://0123456789abcdef0123456789abcdef@o777.ingest.sentry.io/777')

            self.assertIsNone(detect_dsn(root))

    def test_detect_dsn_skips_files_without_dsn_and_handles_multiple_fallbacks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # File with supported suffix but no DSN
            (root / "Normal.swift").write_text("print('hello world')")

            # Two test files matching DSN to hit fallback or branch
            test_dir = root / "tests"
            test_dir.mkdir()
            (test_dir / "Test1.swift").write_text('https://0123456789abcdef0123456789abcdef@o111.ingest.sentry.io/111')
            (test_dir / "Test2.swift").write_text('https://0123456789abcdef0123456789abcdef@o222.ingest.sentry.io/222')

            found = detect_dsn(root)
            self.assertIsNotNone(found)
    def test_detect_dsn_skips_large_files_and_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large_file = root / "Huge.swift"
            large_file.write_text('let dsn = "https://0123456789abcdef0123456789abcdef@o888.ingest.sentry.io/888"')

            # Mock stat st_size > 2_000_000
            orig_stat = Path.stat

            def fake_stat(self_path, *args, **kwargs):
                st = orig_stat(self_path, *args, **kwargs)
                if self_path.name == "Huge.swift":
                    class FakeStat:
                        st_size = 2_500_000
                    return FakeStat()
                return st

            with patch.object(Path, "stat", fake_stat):
                self.assertIsNone(detect_dsn(root))

            # Simulate OSError on read_text
            with patch.object(Path, "read_text", side_effect=OSError("Permission denied")):
                self.assertIsNone(detect_dsn(root))


if __name__ == "__main__":
    unittest.main()
