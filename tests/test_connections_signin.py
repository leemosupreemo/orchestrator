"""Connections: Trello's Allow sign-in, one Sentry setup for connections and device logs, and token expiry."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator import integrations as ig  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402


class ExpiryTests(unittest.TestCase):
    def test_tokens_that_expire_record_when_capped_at_the_services_maximum(self):
        with patch.object(ig.Figma, "test", return_value="me"):
            creds, _ = ig.connect("figma", {"token": "figd_x"})
            self.assertEqual(creds["expires"], (date.today() + timedelta(days=90)).isoformat())  # the longest it can live
            later = (date.today() + timedelta(days=400)).isoformat()
            creds, _ = ig.connect("figma", {"token": "figd_x", "expires": later})
            self.assertEqual(creds["expires"], (date.today() + timedelta(days=90)).isoformat())  # never past the maximum
            with self.assertRaises(ig.IntegrationError):
                ig.connect("figma", {"token": "figd_x", "expires": "2001-01-01"})
        with patch.object(ig.Sentry, "test", return_value="me"):
            creds, _ = ig.connect("sentry", {"org": "acme", "token": "t"})
            self.assertNotIn("expires", creds)  # Sentry's personal tokens don't expire on a timer

    def test_the_catalog_warns_before_and_after(self):
        soon = (date.today() + timedelta(days=5)).isoformat()
        figma = next(p for p in ig.public_catalog({"figma": {"token": "x", "expires": soon}}) if p["id"] == "figma")
        self.assertEqual((figma["expiry"]["days_left"], figma["expiry"]["soon"], figma["token_max_days"]), (5, True, 90))
        self.assertTrue(ig.expiry_view("2001-01-01")["expired"])
        self.assertIsNone(ig.expiry_view(""))


class TrelloSignInTests(unittest.TestCase):
    def test_the_allow_button_appears_only_with_an_app_key(self):
        trello = lambda settings=None: next(p for p in ig.public_catalog({}, settings=settings) if p["id"] == "trello")  # noqa: E731
        with patch.dict(os.environ, {"ORCHESTRATOR_TRELLO_APP_KEY": ""}):
            self.assertNotIn("authorize_key", trello())
            self.assertEqual(trello({"trello_app_key": "abc123"})["authorize_key"], "abc123")

    def test_the_return_page_has_no_inline_script(self):
        page = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "trello-auth.html").read_text()
        self.assertIn('<script src="trello-auth.js"></script>', page)  # the server's policy blocks inline scripts
        self.assertEqual(page.count("<script"), 1)


class FigmaSignInTests(unittest.TestCase):
    def test_the_oauth_button_appears_with_client_id(self):
        figma = lambda settings=None: next(p for p in ig.public_catalog({}, settings=settings) if p["id"] == "figma")  # noqa: E731
        with patch.dict(os.environ, {"ORCHESTRATOR_FIGMA_CLIENT_ID": ""}):
            self.assertNotIn("authorize_key", figma())
            self.assertEqual(figma({"figma_client_id": "fig_client_123"})["authorize_key"], "fig_client_123")

    def test_the_return_page_has_no_inline_script(self):
        page = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "figma-auth.html").read_text()
        self.assertIn('<script src="figma-auth.js"></script>', page)  # the server's policy blocks inline scripts
        self.assertEqual(page.count("<script"), 1)

    def test_oauth_code_exchange_and_bearer_headers(self):
        token_response = {"access_token": "figo_secret_access", "refresh_token": "r_123", "expires_in": 7776000}
        with patch.object(ig, "exchange_figma_token", return_value=token_response) as mock_exchange, \
             patch.object(ig.Figma, "test", return_value="designer_bob"):
            creds, who = ig.connect("figma", {"code": "c_123", "code_verifier": "v_abc", "client_id": "cid_xyz", "redirect_uri": "https://app/figma-auth.html"})
            self.assertEqual(who, "designer_bob")
            self.assertEqual(creds["token"], "figo_secret_access")
            self.assertEqual(creds["auth_type"], "oauth")
            self.assertEqual(creds["refresh_token"], "r_123")
            mock_exchange.assert_called_once_with(code="c_123", code_verifier="v_abc", redirect_uri="https://app/figma-auth.html", client_id="cid_xyz", client_secret="")

        # Bearer header for oauth
        f_oauth = ig.Figma({"token": "figo_secret_access", "auth_type": "oauth"})
        self.assertEqual(f_oauth._headers(), {"Authorization": "Bearer figo_secret_access"})

        # X-Figma-Token header for personal access token
        f_pat = ig.Figma({"token": "figd_pat_token"})
        self.assertEqual(f_pat._headers(), {"X-Figma-Token": "figd_pat_token"})


class RejectedTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".orchestrator" / "config").mkdir(parents=True)
        (self.root / ".orchestrator" / "config" / "settings.json").write_text(json.dumps({"integrations": {"jira": {"site": "x"}}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_turned_down_token_asks_for_reconnecting_and_a_new_one_clears_it(self):
        err = ui.integration_error(ig.RejectedCredentials("The credentials were rejected."), self.root, "jira")
        self.assertIn("Reconnect Jira under Connections", str(err))
        settings = ui.read_settings(self.root)
        self.assertIn("rejected_at", settings["integration_status"]["jira"])
        jira = next(p for p in ig.public_catalog(ui.saved_integrations(self.root), status=settings["integration_status"]) if p["id"] == "jira")
        self.assertTrue(jira["rejected"])
        with patch.object(ig.Jira, "test", return_value="me"):
            ui.connect_integration(self.root, "jira", {"site": "https://x.atlassian.net", "email": "a@b.c", "token": "new"})
        self.assertNotIn("jira", ui.read_settings(self.root).get("integration_status", {}))

    def test_other_errors_dont(self):
        ui.integration_error(ig.IntegrationError("Not found."), self.root, "jira")
        self.assertNotIn("integration_status", ui.read_settings(self.root))


class OneSentrySetupTests(unittest.TestCase):
    def test_the_connect_form_is_filled_from_the_projects_dsn(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "App").mkdir()
            (root / "App" / "Config.swift").write_text('let dsn = "https://0123456789abcdef0123@o4242.ingest.us.sentry.io/77"')
            ui._SENTRY_SUGGEST.clear()
            self.assertEqual(ui.sentry_suggestion(root), {"host": "us.sentry.io", "org": "4242", "project": "77"})

    def test_device_logs_use_the_connected_sentry_account(self):
        sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))
        import cloud_logs
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp)
            (runtime / "config").mkdir()
            (runtime / "config" / "settings.json").write_text(json.dumps({"integrations": {"sentry": {
                "host": "us.sentry.io", "org": "acme", "project": "77", "token": "sntryu_secret"}}}))
            with patch.object(cloud_logs, "ORCHESTRATOR_RUNTIME_DIR", runtime), \
                 patch.object(cloud_logs, "PROJECT_CONFIG", SimpleNamespace(remote_logs={})), \
                 patch.object(cloud_logs, "load_secrets", lambda: None), \
                 patch.dict(os.environ, {"SENTRY_LOGS_TOKEN": "", "SENTRY_AUTH_TOKEN": ""}):
                config = cloud_logs.load_config()
                self.assertEqual((config.api_base, config.org, config.project), ("https://us.sentry.io", "acme", "77"))
                self.assertEqual(config.token(), "sntryu_secret")


class ConnectionUIBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.app_js = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        self.style_css = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "style.css").read_text()

    def test_connect_buttons_do_not_show_as_active_unless_requisite_info_present(self):
        # Card buttons don't apply primary active style to unconfigured "Connect" button
        self.assertIn(': `<button class="btn small ${p.rejected || p.expiry?.expired || p.expiry?.soon ? "primary" : ""}" data-conn-connect="${esc(p.id)}">${!p.connected ? "Connect"', self.app_js)
        # Modal dialog connect state validator disables submit until manual fields are valid
        self.assertIn("updateDialogConnectState", self.app_js)
        self.assertIn("okBtn.disabled = !manualValid;", self.app_js)
        # Figma and Trello quick OAuth buttons in dialog are disabled without client id / key
        self.assertIn("quickFigma.disabled = !cid;", self.app_js)
        self.assertIn("quickTrello.disabled = !k;", self.app_js)

    def test_job_type_buttons_dimmed_by_about_25_percent_on_select(self):
        self.assertIn(".job-type-card:has(input:checked)::before {\n  opacity: 0.62;", self.style_css)
        self.assertIn("brightness(0.95);", self.style_css)
        self.assertIn(".job-type-card:has(input:checked)::before { opacity: 0.68;", self.style_css)

    def test_new_job_page_has_no_tell_it_what_you_need_subtitle(self):
        self.assertNotIn("Tell it what you need. It's planned, then built on a worker, and anything that needs you shows up on Home.", self.app_js)

    def test_frameworks_explain_benefit_and_how_to_use_without_generic_intro_text(self):
        self.assertNotIn("Detection checks your test source files and local tools. Expand an item to see what it does and how to get started.", self.app_js)
        self.assertIn("<strong>Benefit:</strong>", self.app_js)
        self.assertIn("<strong>How to use:</strong>", self.app_js)
        self.assertIn(".framework-guide", self.style_css)

    def test_top_header_bar_has_no_logo_and_hamburger_brand_is_aligned(self):
        html = (PACKAGE_ROOT / "orchestrator" / "web" / "static" / "index.html").read_text()
        self.assertNotIn('<a class="brand-home"', html)
        self.assertIn('<span class="brand-mark" role="img" aria-label="Orchestrator"></span>', html)
        self.assertIn('.brand { display: flex; align-items: center; justify-content: space-between; gap: var(--space-3); font-weight: 650; font-size: var(--text-lg); padding: 0 var(--space-3); }', self.style_css)
        self.assertIn('.sidebar .brand { padding: 0 var(--space-3); }', self.style_css)

    def test_search_shortcut_only_on_mac_pc_desktop_none_on_mobile(self):
        self.assertIn("configureSearchShortcut", self.app_js)
        self.assertIn(".search-btn kbd { display: none !important; }", self.style_css)


if __name__ == "__main__":
    unittest.main()

