from __future__ import annotations

import unittest

from orchestrator import preflight as pf


def probes(installed=(), results=None):
    results = results or {}
    calls = []

    def run(argv):
        calls.append(argv)
        for key, value in results.items():
            if " ".join(argv).startswith(key):
                return value
        return (0, "")

    return run, (lambda tool: tool in installed), calls


def machine(**kw):
    return {"name": "local", "enabled": True, "execution_mode": "local", "models": [], **kw}


def by_id(items):
    return {i["id"]: i for i in items}


class CommandToolsTests(unittest.TestCase):
    def test_finds_the_programs_a_command_line_starts(self):
        self.assertEqual(pf.command_tools("cd app && FOO=1 npm test | tee out.txt"), ["npm", "tee"])
        self.assertEqual(pf.command_tools("python3 -m unittest discover -s tests"), ["python3"])
        self.assertEqual(pf.command_tools("xcodebuild -scheme X test && echo done"), ["xcodebuild"])
        self.assertEqual(pf.command_tools(None), [])
        self.assertEqual(pf.command_tools("unbalanced 'quote"), [])


class GenericChecks(unittest.TestCase):
    def go(self, config=None, machines=None, installed=(), results=None, apple=False, disk=100.0, clis=None, github=True):
        run, which, _ = probes(installed, results)
        return by_id(pf.checks(config or {}, machines if machines is not None else [machine()], apple, disk, run, which,
                               lambda m: (clis or {}).get(m, []), github=github))

    def test_a_missing_build_tool_fails_and_points_at_project_settings(self):
        r = self.go({"build_command": "cargo build", "test_command": "cargo test"}, installed=("gh",))
        self.assertEqual((r["build-tool"]["status"], r["test-tool"]["status"]), ("fail", "fail"))
        self.assertIn("cargo", r["build-tool"]["detail"])
        self.assertEqual(r["build-tool"]["route"], "")  # no page edits the build command; the fix says where it lives
        self.assertIn("build_command", r["build-tool"]["fix"])

    def test_installed_tools_pass(self):
        r = self.go({"build_command": "cargo build"}, installed=("cargo", "gh"))
        self.assertEqual(r["build-tool"]["status"], "ok")

    def test_a_model_whose_cli_is_missing_fails_and_names_the_model(self):
        r = self.go(machines=[machine(models=["opencode/x-free"])], clis={"opencode/x-free": ["opencode"]}, installed=("gh",))
        self.assertEqual(r["model-clis"]["status"], "fail")
        self.assertIn("opencode/x-free", r["model-clis"]["detail"])
        r = self.go(machines=[machine(models=["opencode/x-free"])], clis={"opencode/x-free": ["opencode"]}, installed=("opencode", "gh"))
        self.assertEqual(r["model-clis"]["status"], "ok")

    def test_a_remote_machines_models_are_not_checked_on_this_computer(self):
        r = self.go(machines=[machine(execution_mode="remote", models=["m"])], clis={"m": ["only-on-remote"]})
        self.assertNotIn("model-clis", r)

    def test_a_github_project_cant_start_jobs_without_github(self):
        r = self.go(installed=("gh",), results={"gh auth status": (1, "not logged in")})
        self.assertEqual(r["github"]["status"], "fail")
        self.assertIn("code_host", r["github"]["detail"])  # says how to work without it
        self.assertEqual(self.go(installed=("gh",))["github"]["status"], "ok")
        self.assertEqual(self.go()["github"]["status"], "fail")  # gh not installed

    def test_a_plain_git_project_needs_no_github(self):
        self.assertNotIn("github", self.go(github=False))

    def test_low_disk_blocks_only_when_work_runs_here(self):
        self.assertEqual(self.go(disk=3)["disk"]["status"], "fail")
        self.assertNotIn("disk", self.go(disk=3, machines=[machine(execution_mode="remote")]))
        self.assertNotIn("disk", self.go(disk=None))

    def test_no_apple_checks_for_other_projects(self):
        r = self.go({"build_command": "npm run build"}, installed=("npm", "gh"))
        self.assertFalse({"xcode", "simulator", "xcode-machine"} & set(r))


class AppleChecks(unittest.TestCase):
    def go(self, installed=("xcodebuild", "gh"), results=None, machines=None):
        run, which, calls = probes(installed, results)
        out = by_id(pf.checks({}, machines if machines is not None else [machine(supports_xcode=True)], True, 100, run, which))
        return out, calls

    SIMS = "-- iOS 18.0 --\n    iPhone 16 (ABC) (Shutdown)\n"

    def test_all_good(self):
        r, _ = self.go(results={"xcrun simctl": (0, self.SIMS)})
        self.assertEqual((r["xcode"]["status"], r["simulator"]["status"]), ("ok", "ok"))

    def test_no_xcode(self):
        r, calls = self.go(installed=("gh",))
        self.assertEqual(r["xcode"]["status"], "fail")
        self.assertNotIn("simulator", r)  # nothing else to probe without Xcode
        self.assertEqual(calls, [["gh", "auth", "status"]])

    def test_xcode_without_first_launch_setup(self):
        r, _ = self.go(results={"xcodebuild -checkFirstLaunchStatus": (1, "license not accepted"), "xcrun simctl": (0, self.SIMS)})
        self.assertEqual(r["xcode"]["status"], "fail")
        self.assertIn("runFirstLaunch", r["xcode"]["fix"])

    def test_a_coresimulator_mismatch_is_recognised_before_a_build_hits_it(self):
        r, _ = self.go(results={"xcrun simctl": (1, "CoreSimulator is out of date. Current version (1) is older than build version (2).")})
        self.assertEqual(r["simulator"]["status"], "fail")
        self.assertIn("CoreSimulator", r["simulator"]["detail"])

    def test_no_simulator_runtime_is_a_warning(self):
        r, _ = self.go(results={"xcrun simctl": (0, "== Devices ==\n")})
        self.assertEqual(r["simulator"]["status"], "warn")

    def test_no_machine_marked_for_xcode(self):
        r, _ = self.go(results={"xcrun simctl": (0, self.SIMS)}, machines=[machine(supports_xcode=False)])
        self.assertEqual(r["xcode-machine"]["status"], "fail")
        self.assertEqual(r["xcode-machine"]["route"], "#/config/fleet")


if __name__ == "__main__":
    unittest.main()
