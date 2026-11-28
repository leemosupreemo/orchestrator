"""App delivery & Delivery setup: prerequisites live on the dedicated Delivery setup page (#/delivery/setup) alongside
build/distribution target settings. On App delivery (#/delivery), setup steps appear front and center when something is still
missing (hiding the empty send card), and stay out of the way once everything is set up."""
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text()
CSS = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "style.css").read_text()
HELPERS = APP[APP.index("function buildDeliveryTargetCard("):APP.index("pages.delivery = async")]
PAGE = APP[APP.index("pages.delivery = async"):APP.index('pages["delivery-setup"] = async')]
SETUP_PAGE = APP[APP.index('pages["delivery-setup"] = async'):APP.index("const KPI_STATE =")]


class ContainerMarkupTests(unittest.TestCase):
    def container(self) -> str:
        start = HELPERS.index('const prereqHtml = web && !setupPage ? ""')
        return HELPERS[start:HELPERS.index("</details>`;", start)]

    def test_it_holds_where_builds_go_signing_and_the_server_and_a_place_for_what_is_done(self):
        html = self.container()
        for bit in ('id="prereq-target"', 'id="prereq-signing"', 'id="signing-card"', 'id="prereq-server"', 'id="prereq-todo"',
                    'id="prereq-done"', 'id="prereq-done-list"', "Already set up (", "Before you can send builds"):
            self.assertIn(bit, html)

    def test_a_web_project_has_no_container_on_delivery_and_ready_target_stays_hidden(self):
        html = self.container()
        self.assertIn('const prereqHtml = web && !setupPage ? ""', HELPERS)
        self.assertIn('${setupPage ? " open" : (targetReady ? " hidden" : " open")}', html)

    def test_delivery_page_hides_send_card_when_not_set_up_and_links_to_setup_page(self):
        self.assertIn('id="delivery-send"${!web && !targetReady ? " hidden" : ""}', PAGE)
        self.assertIn('href="#/delivery/setup"', PAGE)
        self.assertNotIn("Advanced delivery settings", PAGE)
        html = PAGE[PAGE.index('html: `${prereqHtml}'):][:300]
        self.assertLess(html.index("${prereqHtml}"), html.index("${branchCard}"))
        self.assertLess(html.index("${branchCard}"), html.index('id="server-card"'))

    def test_setup_page_always_includes_prereqs_and_target_settings(self):
        self.assertIn("setupPage: true", SETUP_PAGE)
        self.assertIn("buildDeliveryTargetCard(", SETUP_PAGE)
        self.assertIn("wireDeliveryTargetForm(view)", SETUP_PAGE)
        self.assertIn('href="#/config/firebase"', SETUP_PAGE)
        self.assertIn('if (parts[0] === "delivery" && parts[1] === "setup") return { page: "delivery-setup"', APP)
        self.assertIn('if (r.page === "delivery-setup") return "#/delivery";', APP)


class StateTests(unittest.TestCase):
    def test_signing_reports_ready_todo_or_not_applicable(self):
        fn = APP[APP.index("async function loadSigningCard("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("onState = () => {}", fn)
        self.assertIn('item.state !== "no-project" && item.state !== "not-needed"', fn)
        self.assertIn('["ready", "env", "unknown"].includes(item.state)', fn)  # "can't tell" isn't nagged about
        self.assertIn("onState(null)", fn)  # a failed load means it doesn't apply, not that it is missing

    def test_a_server_is_a_prerequisite_only_when_builds_start_it_first(self):
        fn = APP[APP.index("async function loadServerCard("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("required: (view.app || []).some((a) => a.id === picked.id && a.machine === view.machine)", fn)
        self.assertIn('picked.status?.state === "running"', fn)
        self.assertIn("onState(null)", fn)
        wiring = HELPERS[HELPERS.index("loadServerCard(serverCard"):][:450]
        self.assertIn("if (s?.required && items.server)", wiring)
        self.assertIn('report("server", null)', wiring)

    def test_the_container_sorts_items_and_hides_all_set_on_delivery_while_keeping_it_on_setup(self):
        wiring = HELPERS[HELPERS.index("const refreshPrereqs = () =>"):HELPERS.index("const report = ")]
        for bit in ('it.ready === false', 'it.ready === true', 'it.ready === null', '`${todo.length} to do` : "All set"',
                    'if (setupPage)', 'box.hidden = !todo.length', 'send.before(box)',
                    'const waiting = ["target", "signing"].some'):
            self.assertIn(bit, wiring)

    def test_the_slow_server_check_is_not_waited_for(self):
        wiring = HELPERS[HELPERS.index("const refreshPrereqs = () =>"):HELPERS.index("const report = ")]
        self.assertNotIn('"server"].some', wiring)


class StyleTests(unittest.TestCase):
    def test_cards_inside_the_container_do_not_add_their_own_spacing(self):
        self.assertIn(".prereqs .card { margin-bottom: 0; box-shadow: none; }", CSS)


class UnityIosPrereqTests(unittest.TestCase):
    def test_detects_missing_and_installed_unity_ios_support(self):
        import os
        import tempfile
        from unittest import mock
        from orchestrator import delivery_setup

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            (root / "DaocUnity" / "Assets").mkdir(parents=True)
            (root / "DaocUnity" / "ProjectSettings").mkdir(parents=True)
            (root / "DaocUnity" / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.6f1\n")
            hub_root = Path(tmp) / "Editors"
            (hub_root / "6000.6.6f1").mkdir(parents=True)
            hub_bin = Path(tmp) / "Unity Hub"
            hub_bin.write_text("#!/bin/sh\n")
            cfg = {"delivery_platform": "file", "delivery_build_command": "bash tools/package_ios.sh", "delivery_artifact": ".local/*.ipa"}

            with mock.patch.dict(os.environ, {"UNITY_HUB_EDITOR_ROOT": str(hub_root), "UNITY_HUB_BIN": str(hub_bin)}):
                status = delivery_setup.unity_ios_status(root, cfg)
                self.assertIsNotNone(status)
                self.assertFalse(status["ready"])
                self.assertTrue(status["can_install"])
                self.assertEqual(status["version"], "6000.6.6f1")

                def fake_run(cmd, **kwargs):
                    (hub_root / "6000.6.6f1" / "PlaybackEngines" / "iOSSupport").mkdir(parents=True)
                    return mock.Mock(returncode=0, stdout="", stderr="")

                with mock.patch("subprocess.run", side_effect=fake_run) as run_mock:
                    res = delivery_setup.install_unity_ios(root, cfg)
                self.assertTrue(res["ok"])
                self.assertEqual(run_mock.call_args[0][0], [str(hub_bin), "--", "--headless", "install-modules", "--version", "6000.6.6f1", "-m", "ios", "--cm"])
                self.assertTrue(delivery_setup.unity_ios_status(root, cfg)["ready"])

    def test_delivery_page_offers_modal_approved_unity_ios_install(self):
        for bit in ('id="prereq-unity"', 'id="install-unity-ios-btn"', 'formDialog(\n      "Install Unity iOS Build Support?"',
                    'api("delivery/unity-ios"', 'confirmed: true'):
            self.assertIn(bit, HELPERS)


if __name__ == "__main__":
    unittest.main()
