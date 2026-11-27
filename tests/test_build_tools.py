"""End-to-end dependency readiness across all 5 layers:
1. Host toolchains & pinned version constraints (`build_tools`)
2. Repo / manifest lockfile hydration (`manifest_deps`)
3. Platform SDKs, licenses & build modules (Unity, Android, Xcode)
4. Declarative project `requires` (`docker`, `container:`, `command:`, `path:`, `env:`, `device:ios`)
5. Capability-aware peer failover & non-spammy 1-click catch-all UI (`setup_environment`)
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orchestrator import ai_providers, blockers, build_tools, config_validation, manifest_deps, preflight, project_config, run_result, test_commands
from orchestrator.web import server as web_server

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "orchestrator" / "web" / "static"


class BuildToolsAndVersionTests(unittest.TestCase):
    def test_tool_dirs_include_dotnet_install_locations(self):
        path_str = ai_providers.tool_path("/usr/bin")
        self.assertIn("/opt/homebrew/bin", path_str)
        self.assertIn("/usr/local/share/dotnet", path_str)
        self.assertTrue(path_str.endswith("/.dotnet") or "/.dotnet:" in path_str)

    def test_describe_tool_recognizes_dotnet_unity_modules_docker_and_manual_tools(self):
        dotnet = build_tools.describe_tool("dotnet")
        self.assertEqual((dotnet["id"], dotnet["name"], dotnet["installable"], dotnet["method"]),
                         ("dotnet", ".NET SDK", True, "Homebrew"))

        unity_path = "/Applications/Unity/Hub/Editor/6000.6.6f1/Unity.app/Contents/MacOS/Unity"
        u_hub = build_tools.describe_tool(unity_path, unity_hub_installed=True)
        self.assertEqual((u_hub["id"], u_hub["name"], u_hub["installable"], u_hub["method"]),
                         ("unity:6000.6.6f1", "Unity 6000.6.6f1", True, "Unity Hub"))
        u_mod = build_tools.describe_tool("unity-module:ios:6000.6.6f1", unity_hub_installed=True)
        self.assertEqual((u_mod["id"], u_mod["installable"], u_mod["method"]),
                         ("unity-module:ios:6000.6.6f1", True, "Unity Hub"))
        u_no_hub = build_tools.describe_tool(unity_path, unity_hub_installed=False)
        self.assertFalse(u_no_hub["installable"])
        self.assertIn("Unity Hub", u_no_hub["manual_hint"])

        npm = build_tools.describe_tool("npm")
        self.assertEqual((npm["id"], npm["name"], npm["installable"]), ("node", "Node.js", True))

        xcode = build_tools.describe_tool("xcodebuild")
        self.assertEqual((xcode["id"], xcode["installable"]), ("xcodebuild", False))

    def test_missing_project_tools_detects_dotnet_and_unity_from_project_config(self):
        cfg = {
            "build_command": '"${UNITY_PATH:-/Applications/Unity/Hub/Editor/6000.6.6f1/Unity.app/Contents/MacOS/Unity}" -batchmode -quit',
            "test_command": 'dotnet test "tests/A.csproj" && python3 -m unittest && dotnet test "tests/B.csproj"',
        }
        missing = build_tools.missing_project_tools(cfg, which=lambda t: t == "python3", env={})
        self.assertEqual([m["id"] for m in missing], ["unity:6000.6.6f1", "dotnet"])
        self.assertEqual(missing[0]["needed_by"], ["build"])
        self.assertEqual(missing[1]["needed_by"], ["test"])

    def test_install_tool_rejects_unlisted_or_unsafe_ids(self):
        self.assertFalse(build_tools.is_valid_install_id("dotnet; rm -rf /"))
        self.assertFalse(build_tools.is_valid_install_id("unity:../evil"))
        self.assertFalse(build_tools.is_valid_install_id("unity-module:tvos:6000.6.6f1"))
        self.assertTrue(build_tools.is_valid_install_id("dotnet"))
        self.assertTrue(build_tools.is_valid_install_id("unity:6000.6.6f1"))
        self.assertTrue(build_tools.is_valid_install_id("unity-module:ios:6000.6.6f1"))
        self.assertFalse(build_tools.install_tool("rm -rf /")["ok"])

    def test_install_tool_via_homebrew_and_unity_hub(self):
        installed = {"dotnet": False}
        calls = []

        def fake_which(name: str) -> bool:
            return True if name == "brew" else installed.get(name, False)

        def fake_run(argv: list[str], timeout: int):
            calls.append(argv)
            installed["dotnet"] = True
            return 0, "==> Pouring dotnet--10.0.402\n", False

        lines: list[str] = []
        res = build_tools.install_tool("dotnet", lines.append, run=fake_run, which=fake_which)
        self.assertTrue(res["ok"])
        self.assertEqual(calls, [["brew", "install", "dotnet"]])
        summary = run_result.summarize("install_tool", "\n".join(lines), 0)
        self.assertEqual(summary["tone"], "ok")
        self.assertIn(".NET SDK is installed", summary["headline"])

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "Unity Hub"
            editors = Path(tmp) / "Editors"
            hub.write_text("#!/bin/sh\n", encoding="utf-8")
            present = set()
            u_calls = []

            def fake_u_run(argv: list[str], timeout: int):
                u_calls.append(argv)
                present.add("/Applications/Unity/Hub/Editor/6000.6.6f1/Unity.app/Contents/MacOS/Unity")
                return 0, "Installed 6000.6.6f1\n", False

            res_ed = build_tools.install_tool("unity:6000.6.6f1", run=fake_u_run, which=lambda t: t in present, unity_hub_bin=hub, unity_editors_root=editors)
            self.assertTrue(res_ed["ok"])
            res_mod = build_tools.install_tool("unity-module:ios:6000.6.6f1", run=fake_u_run, which=lambda _: True, unity_hub_bin=hub, unity_editors_root=editors)
            self.assertTrue(res_mod["ok"])
            self.assertEqual(u_calls[1], [str(hub), "--", "--headless", "install-modules", "--version", "6000.6.6f1", "-m", "ios", "--childModules"])

    def test_toolchain_version_pins_checked_against_installed_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "global.json").write_text(json.dumps({"sdk": {"version": "8.0.100", "rollForward": "latestFeature"}}), encoding="utf-8")
            (root / ".nvmrc").write_text("20\n", encoding="utf-8")
            (root / ".python-version").write_text("3.12\n", encoding="utf-8")
            (root / "go.mod").write_text("module example.com/m\n\ngo 1.23\n", encoding="utf-8")

            def fake_run(argv: list[str]):
                if argv[0] == "dotnet":
                    return 0, "10.0.402 [/opt/homebrew]\n"
                if argv[0] == "node":
                    return 0, "v18.19.0\n"
                if argv[0] == "python3":
                    return 0, "Python 3.12.4\n"
                if argv[0] == "go":
                    return 0, "go version go1.21.5 darwin/arm64\n"
                return 0, ""

            findings = build_tools.check_toolchain_versions(root, fake_run, lambda _: True)
            ids = [f["id"] for f in findings]
            self.assertEqual(ids, ["version-dotnet", "version-node", "version-go"])  # Python 3.12.4 satisfies 3.12

    def test_platform_sdk_checks_unity_license_modules_and_android_sdk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            home = Path(tmp) / "home"
            (root / "ProjectSettings").mkdir(parents=True)
            (root / "Assets").mkdir()
            (root / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 6000.6.6f1\n", encoding="utf-8")
            (root / "app" / "src" / "main").mkdir(parents=True)
            (root / "app" / "src" / "main" / "AndroidManifest.xml").write_text("<manifest/>", encoding="utf-8")

            findings = build_tools.check_platform_sdks(
                root,
                {"delivery_artifact": ".local/release/*.ipa"},
                which=lambda t: "Unity" in t,
                env={},
                home=home,
                unity_editors_root=Path(tmp) / "Editors",
            )
            by_id = {f["id"]: f for f in findings}
            self.assertEqual(by_id["unity-license"]["status"], "warn")
            self.assertEqual(by_id["unity-module-ios"]["status"], "fail")
            self.assertEqual(by_id["android-jdk"]["status"], "fail")
            self.assertEqual(by_id["android-sdk"]["status"], "fail")


class ManifestHydrationAndUnifiedSetupTests(unittest.TestCase):
    def test_unhydrated_manifests_detects_lockfiles_without_flagging_stdlib_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Plain stdlib Python project without lockfiles -> empty
            self.assertEqual(manifest_deps.unhydrated_manifests(root, {"test_command": "python3 -m unittest"}), [])

            # Add package.json + package-lock.json, uv.lock, and Podfile.lock
            (root / "package.json").write_text(json.dumps({"dependencies": {"react": "^19.0.0"}}), encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (root / "Podfile.lock").write_text("PODS:\n", encoding="utf-8")

            found = manifest_deps.unhydrated_manifests(root, {})
            self.assertEqual([m["id"] for m in found], ["manifest:npm", "manifest:uv", "manifest:cocoapods"])

            # Once hydrated on disk, they clear
            (root / "node_modules").mkdir()
            (root / ".venv").mkdir()
            (root / "Pods").mkdir()
            (root / "Pods" / "Manifest.lock").write_text("PODS:\n", encoding="utf-8")
            self.assertEqual(manifest_deps.unhydrated_manifests(root, {}), [])

    def test_project_requires_and_catch_all_setup_consolidate_without_spam(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2.0.0"}}), encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            (root / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16\n", encoding="utf-8")

            cfg = {
                "project_name": "demo",
                "code_host": "git",
                "build_command": "dotnet build",
                "test_command": "npm test",
                "requires": ["container:demo-db", "env:DEMO_API_KEY", "device:ios"],
            }
            items = preflight.checks(
                cfg,
                machines=[],
                is_apple_app=False,
                disk_free_gb=50.0,
                run=lambda argv: (1, ""),
                which=lambda t: t in ("npm", "node", "docker"),
                github=False,
                env={},
                root=root,
            )
            plan = manifest_deps.summarize_environment_setup(items)
            self.assertFalse(plan["ready"])
            # Single catch-all button label despite multiple failing layers!
            self.assertEqual(plan["button_label"], "Set up environment…")
            auto_kinds = [(s["kind"], s["id"]) for s in plan["auto_steps"]]
            self.assertIn(("tool", "dotnet"), auto_kinds)
            self.assertIn(("manifest", "manifest:npm"), auto_kinds)
            self.assertTrue(any(k == "server" for k, _ in auto_kinds))
            manual_ids = [m["id"] for m in plan["manual_items"]]
            self.assertIn("req-env-DEMO_API_KEY", manual_ids)
            self.assertIn("req-device-ios", manual_ids)

    def test_setup_environment_runs_tools_then_manifests_in_one_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text(json.dumps({"dependencies": {"zod": "^3.0.0"}}), encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            have = {"node", "npm"}
            installed_tools: list[str] = []
            ran_cmds: list[list[str]] = []

            def fake_install(tid: str, say):
                installed_tools.append(tid)
                have.add(tid)
                say(f"Installed {tid}")
                return {"ok": True, "tool": tid, "message": "ok"}

            def fake_exec(argv: list[str], cwd: Path, timeout: int):
                ran_cmds.append(argv)
                if argv == ["npm", "ci"]:
                    (root / "node_modules").mkdir(exist_ok=True)
                return 0, "added 12 packages\n", False

            logs: list[str] = []
            res = manifest_deps.setup_environment(
                root,
                {"build_command": "dotnet build", "test_command": "npm test"},
                logs.append,
                tool_installer=fake_install,
                exec_in_dir=fake_exec,
                which=lambda t: t in have,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(installed_tools, ["dotnet"])
            self.assertIn(["npm", "ci"], ran_cmds)
            summary = run_result.summarize("setup_environment", "\n".join(logs), 0)
            self.assertEqual(summary["tone"], "ok")

    def test_config_validation_validates_top_level_requires_and_device_ios(self):
        reqs, errs = test_commands.parse_requires(["docker", "container:db", "device:ios", "env:TOKEN"])
        self.assertEqual(errs, [])
        self.assertEqual(len(reqs), 4)
        _, bad_errs = test_commands.parse_requires(["device:watchos", "bogus:x"])
        self.assertEqual(len(bad_errs), 2)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator").mkdir()
            (root / ".orchestrator" / "project.json").write_text(json.dumps({
                "project_name": "p",
                "code_host": "git",
                "build_command": "python3 -m compileall .",
                "test_command": "python3 -m unittest",
                "requires": ["invalid-req"],
            }), encoding="utf-8")
            cfg = project_config.load_project_config(root)
            val_errors = config_validation.validate_project_config(cfg)
            self.assertTrue(any("unknown requirement" in e for e in val_errors))


class ServerWorkerAndUiIntegrationTests(unittest.TestCase):
    def test_server_exposes_environment_setup_and_setup_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator" / "config").mkdir(parents=True)
            (root / ".orchestrator" / "project.json").write_text(
                json.dumps({"test_command": "dotnet test tests/A.csproj", "code_host": "git"}),
                encoding="utf-8",
            )
            (root / ".orchestrator" / "config" / "machines.json").write_text(
                json.dumps({"machines": [{"name": "local", "enabled": True, "execution_mode": "local", "models": ["m1"]}]}),
                encoding="utf-8",
            )
            fake_preflight = [{
                "id": "test-tool",
                "status": "fail",
                "title": "The test command can't run",
                "detail": "This computer couldn't find an executable for dotnet.",
                "fix": "Install the tool",
                "route": "",
                "missing_tools": [build_tools.describe_tool("dotnet")],
            }]
            with patch.object(web_server, "preflight_overview", return_value=fake_preflight):
                planned_job = {"job_id": "j0", "status": "planned", "type": "feature-plan"}
                blk = web_server.job_blockers(root, planned_job)
                self.assertTrue(any(b["id"] == "test-tool" and b["missing_tools"][0]["id"] == "dotnet" for b in blk))
                env_setup = web_server.environment_setup_for(root)
                self.assertFalse(env_setup["ready"])
                self.assertEqual(env_setup["button_label"], "Install .NET SDK…")

            self.assertEqual(web_server.build_setup_environment({}, root)[-1], "setup-environment")
            self.assertEqual(web_server.build_install_tool({"tool": "dotnet"}, root)[-2:], ["install-tool", "dotnet"])
            with self.assertRaises(web_server.UIError):
                web_server.build_install_tool({"tool": "bogus-shell;id"}, root)

    def test_worker_startup_gate_catches_missing_tools_and_unhydrated_manifests(self):
        import sys
        sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))
        import worker_run as worker

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".orchestrator").mkdir()
            (root / ".orchestrator" / "project.json").write_text(
                json.dumps({"test_command": "python3 -m unittest"}),
                encoding="utf-8",
            )
            (root / "package.json").write_text(json.dumps({"dependencies": {"express": "^5.0.0"}}), encoding="utf-8")
            (root / "package-lock.json").write_text("{}", encoding="utf-8")
            with patch.object(worker, "ROOT", root):
                blk = worker.missing_build_tool_blocker({"job_id": "j1", "type": "feature-plan"})
                self.assertIsNotNone(blk)
                self.assertEqual(blk["title"], "Project packages aren't installed yet")

    def test_ui_renders_single_catch_all_button_without_per_dependency_spam(self):
        home_js = (STATIC / "home-status.js").read_text(encoding="utf-8")
        html_js = (STATIC / "html.js").read_text(encoding="utf-8")
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("async function promptEnvironmentSetup(", app_js)
        self.assertIn('data-setup-env="1"', app_js)
        self.assertIn('["install_tool", "setup_environment"].includes(r.action) && r.job', app_js)
        self.assertIn("const machineBlocker = diskBlocked || (typeof toolBlocked !== \"undefined\" && toolBlocked) ||", app_js)

        # Verify jobStartBlockers emits ONE catch-all button even when 3 tools/manifests fail
        helpers_src = app_js[app_js.index("function installToolButtonHtml("):app_js.index("async function confirmServerAction(")]
        blockers_src = app_js[app_js.index("function jobStartBlockers("):app_js.index("function strandedNote(")]
        snippet = f"{helpers_src}\n{blockers_src}"
        script = f"""
{html_js}
{home_js}
const esc = globalThis.Html.escape;
const state = {{ you: {{ role: "owner" }}, project: {{ computer: {{ name: "Mac" }} }} }};
const document = {{ addEventListener() {{}} }};
{snippet}
const startupHtml = jobStartBlockers([
  {{ id: "build-tool", text: "Missing dotnet", missing_tools: [{{ id: "dotnet", name: ".NET SDK", installable: true }}] }},
  {{ id: "test-tool", text: "Missing node", missing_tools: [{{ id: "node", name: "Node.js", installable: true }}] }},
  {{ id: "manifest-deps", text: "Missing npm packages" }},
], true, "j1", {{
  ready: false,
  title: "3 dependencies needed on this computer",
  detail: "Needs .NET SDK, Node.js, npm packages.",
  button_label: "Set up environment…",
  auto_steps: [
    {{ kind: "tool", id: "dotnet", name: ".NET SDK" }},
    {{ kind: "tool", id: "node", name: "Node.js" }},
    {{ kind: "manifest", id: "manifest:npm", name: "npm packages" }},
  ],
  manual_items: [],
}});
const homeHtml = globalThis.HomeStatus.render({{
  git: {{}}, jobs: [], runs: [],
  blockers: [{{
    kind: "tool", key: "dotnet", title: ".NET SDK isn't installed", cleared: false,
    tool: {{ id: "dotnet", name: ".NET SDK", installable: true }},
    jobs: [{{ id: "j1", title: "Camelot Hills" }}],
  }}],
}});
console.log(JSON.stringify({{ startupHtml, homeHtml }}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        # Exactly ONE setup button in startupHtml (no per-tool button spam!)
        self.assertEqual(out["startupHtml"].count("data-setup-env="), 1)
        self.assertEqual(out["startupHtml"].count("data-install-tool="), 0)
        self.assertIn("Set up environment…", out["startupHtml"])
        self.assertIn("Install .NET SDK…", out["homeHtml"])

    def test_docker_install_run_status_deduplicates_headline_and_stretches_banner(self):
        # 1. Backend summarizer strips duplicated 'Docker is running.' prefix and omits redundant auto-start sub-bullet
        summary = run_result.summarize(
            "server_install_docker",
            "Install Docker …\n  Docker is running.\nDone: Docker is running. Docker is installed and running.\n",
            0,
        )
        self.assertEqual(summary["headline"], "Docker is installed and running")
        self.assertEqual(summary["details"], [])

        # 2. CSS stretches .banner.run-status full width rather than centering flex-column items
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn(".banner.run-status { align-items: stretch; flex-wrap: nowrap; }", css)
        self.assertIn("width: 100%;", css[css.index(".run-status-details {"):css.index(".run-status-details {") + 260])

        # 3. Frontend runStatus cleans legacy saved headlines and handles missing/ISO timestamps cleanly
        app_js = (STATIC / "app.js").read_text(encoding="utf-8")
        html_js = (STATIC / "html.js").read_text(encoding="utf-8")
        dur_start = app_js.index("function runDuration(")
        dur_src = app_js[dur_start:app_js.index("function runFailureGuidance(", dur_start)]
        rs_start = app_js.index("function runStatus(")
        rs_src = app_js[rs_start:app_js.index("function wireKeychainUnlock(", rs_start)]
        script = f"""
{html_js}
const esc = globalThis.Html.escape;
const plural = (n, w) => `${{n}} ${{n === 1 ? w : w + "s"}}`;
const act = (a) => `data-action="${{a}}"`;
const stoppingRuns = new Set();
const state = {{ disk_low: false, disk_free_bytes: 50 * (1024 ** 3) }};
const pendingUpdate = false;
const runFailureGuidance = () => null;
{dur_src}
{rs_src}
const html = runStatus({{
  id: "r-dock",
  action: "server_install_docker",
  running: false,
  exit_code: 0,
  started: 1000,
  ended: 1018,
  progress: {{ machines: ["MacBook-Air-6"] }},
  result: {{ headline: "Docker is running. Docker is installed and running.", details: [], tone: "ok" }},
}});
const htmlNoTs = runStatus({{
  id: "r-nots",
  action: "server_install_docker",
  running: false,
  exit_code: 0,
  progress: {{ machines: ["MacBook-Air-6"] }},
  result: {{ headline: "Docker is installed and running", details: [], tone: "ok" }},
}});
console.log(JSON.stringify({{ html, htmlNoTs }}));
"""
        out = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)
        self.assertIn('<strong class="run-status-stage">Docker is installed and running</strong>', out["html"])
        self.assertNotIn("Docker is running. Docker", out["html"])
        self.assertIn("<dd data-run-elapsed>18s</dd>", out["html"])
        self.assertIn("<dd data-run-elapsed>—</dd>", out["htmlNoTs"])


if __name__ == "__main__":
    unittest.main()
