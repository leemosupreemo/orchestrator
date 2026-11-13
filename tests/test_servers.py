from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from orchestrator import servers  # noqa: E402
from test_test_groups import write  # noqa: E402

COMPOSE = """\
services:
  db:
    image: mariadb
    environment:
      MYSQL_ROOT_PASSWORD: super-secret-value
    ports:
      - "3307:3306"
  gameserver:
    image: game
    ports:
      - "10300:10300"
      - 10400:10400/tcp
      - "127.0.0.1:9000:9000"
volumes:
  data:
"""
OVERRIDE = """\
services:
  gameserver:
    ports:
      - "10500:10500"
  extra:
    image: x
"""


def row(service, state="running", health="", ports=()):
    return {"Service": service, "State": state, "Health": health, "Status": state, "Publishers": [{"PublishedPort": p} for p in ports]}


class FakeShell:
    """Answers commands from a table of (substring -> (code, output)); records every command it is asked to run."""

    def __init__(self, replies=None, docker=True):
        self.replies = replies or {}
        self.docker = docker
        self.commands: list[tuple[str, str]] = []
        self.ps_calls = 0
        self.ps_sequence: list[list[dict]] | None = None

    def run(self, command, cwd, timeout):
        self.commands.append((command, cwd))
        if command == "docker info":
            return (0 if self.docker else 1), "", False
        if command.startswith("open -a Docker"):
            self.docker = True
            return 0, "", False
        if " ps -a --format json" in command and self.ps_sequence is not None:
            rows = self.ps_sequence[min(self.ps_calls, len(self.ps_sequence) - 1)]
            self.ps_calls += 1
            return 0, json.dumps(rows), False
        for needle, (code, output) in self.replies.items():
            if needle in command:
                return code, output, False
        return 0, "", False


SERVER = {"id": "srv", "name": "srv", "dir": "srv", "file": None, "files": [], "services": ["db", "gameserver"], "ports": []}


class FindTests(unittest.TestCase):
    def test_compose_files_are_found_with_overrides_merged_nested_repos_included_and_hidden_folders_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "OpenDAoC-Core/.git", "gitdir: x")
            write(root, "OpenDAoC-Core/docker-compose.yml", COMPOSE)
            write(root, "OpenDAoC-Core/docker-compose.override.yml", OVERRIDE)
            write(root, "server/compose.profile.yml", "services:\n  db:\n    image: x\n  server:\n    image: y\n")
            write(root, ".server-build/source/docker-compose.yml", COMPOSE)  # a hidden build copy
            write(root, "node_modules/pkg/docker-compose.yml", COMPOSE)
            found = {s["name"]: s for s in servers.find(root)}
            self.assertEqual(set(found), {"OpenDAoC-Core", "server (compose.profile.yml)"})
            core = found["OpenDAoC-Core"]
            self.assertEqual((core["file"], core["services"]), (None, ["db", "gameserver", "extra"]))
            self.assertEqual(core["ports"], [3307, 10300, 10400, 9000, 10500])
            profile = found["server (compose.profile.yml)"]
            self.assertEqual((profile["file"], profile["services"]), ("compose.profile.yml", ["db", "server"]))
            self.assertEqual(len({s["id"] for s in servers.find(root)}), 2)

    def test_environment_values_are_never_read(self):
        services, ports = servers.parse_compose(COMPOSE)
        self.assertNotIn("secret", json.dumps([services, ports]))

    def test_a_note_appears_when_the_project_mentions_a_protected_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(servers.protected_note(root))
            write(root, "AGENTS.md", "Never run tests against the baseline server on port 10300.")
            self.assertIn("baseline", servers.protected_note(root))


class StatusTests(unittest.TestCase):
    def test_parse_reads_an_array_or_one_object_per_line(self):
        rows = [row("db", ports=[3307]), row("gameserver", "exited")]
        self.assertEqual([c["service"] for c in servers.parse_ps(json.dumps(rows))], ["db", "gameserver"])
        self.assertEqual([c["service"] for c in servers.parse_ps("\n".join(json.dumps(r) for r in rows))], ["db", "gameserver"])
        self.assertEqual(servers.parse_ps(""), [])
        self.assertEqual(servers.parse_ps(json.dumps(rows))[0]["ports"], [3307])

    def test_the_overall_state(self):
        def state(*rows):
            return servers.summarize(["db", "gameserver"], servers.parse_ps(json.dumps(list(rows))))
        self.assertEqual(state(), "stopped")
        self.assertEqual(state(row("db", "exited"), row("gameserver", "exited")), "stopped")
        self.assertEqual(state(row("db"), row("gameserver")), "running")
        self.assertEqual(state(row("db"), row("gameserver", "exited")), "partial")
        self.assertEqual(state(row("db")), "partial")  # the other service was never created
        self.assertEqual(state(row("db", health="healthy"), row("gameserver", health="starting")), "starting")
        self.assertEqual(state(row("db", health="unhealthy"), row("gameserver")), "unhealthy")
        self.assertEqual(state(row("db"), row("gameserver", "restarting")), "unhealthy")

    def test_status_says_when_docker_is_off_or_does_not_answer(self):
        self.assertEqual(servers.status(SERVER, FakeShell(docker=False))["state"], "docker-off")
        bad = servers.status(SERVER, FakeShell({"ps -a": (1, "no configuration file provided: not found")}))
        self.assertEqual((bad["state"], bad["message"]), ("unreachable", "no configuration file provided: not found"))
        shell = FakeShell()
        shell.ps_sequence = [[row("db"), row("gameserver")]]
        self.assertEqual(servers.status(SERVER, shell)["state"], "running")
        self.assertEqual(shell.commands[-1], ("docker compose ps -a --format json", "srv"))  # read-only, in the server's folder

    def test_a_named_compose_file_is_passed_with_dash_f_and_quoted(self):
        self.assertEqual(servers._compose({"file": "compose.profile.yml"}, "ps"), "docker compose -f compose.profile.yml ps")
        self.assertEqual(servers._compose({"file": "weird name.yml"}, "ps"), "docker compose -f 'weird name.yml' ps")


class ChangeTests(unittest.TestCase):
    def say_into(self):
        said: list[str] = []
        return said, said.append

    def test_start_runs_up_detached_and_waits_for_healthy(self):
        shell = FakeShell()
        shell.ps_sequence = [[row("db", health="starting")], [row("db", health="starting"), row("gameserver")], [row("db", health="healthy"), row("gameserver")]]
        said, say = self.say_into()
        result = servers.change(SERVER, "start", shell, say, sleep=lambda s: None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "running")
        self.assertIn("docker compose up -d", [c for c, _ in shell.commands])
        self.assertTrue(any("Waiting" in m for m in said))

    def test_start_reports_a_service_that_never_gets_healthy(self):
        shell = FakeShell()
        shell.ps_sequence = [[row("db", health="unhealthy"), row("gameserver")]]
        result = servers.change(SERVER, "start", shell, sleep=lambda s: None)
        self.assertEqual((result["ok"], result["state"]), (False, "unhealthy"))
        self.assertIn("unhealthy", result["message"])

    def test_a_failed_compose_command_is_reported_with_the_last_line(self):
        shell = FakeShell({"up -d": (1, "pulling\nError response from daemon: port is already allocated")})
        result = servers.change(SERVER, "start", shell, sleep=lambda s: None)
        self.assertFalse(result["ok"])
        self.assertIn("port is already allocated", result["message"])

    def test_stop_and_restart(self):
        shell = FakeShell()
        shell.ps_sequence = [[row("db", "exited"), row("gameserver", "exited")]]
        self.assertTrue(servers.change(SERVER, "stop", shell)["ok"])
        self.assertIn("docker compose stop", [c for c, _ in shell.commands])
        shell = FakeShell()
        shell.ps_sequence = [[row("db"), row("gameserver")]]
        self.assertTrue(servers.change(SERVER, "restart", shell, sleep=lambda s: None)["ok"])
        self.assertIn("docker compose restart", [c for c, _ in shell.commands])
        self.assertTrue(servers.change(SERVER, "stop", FakeShell(docker=False))["ok"])  # nothing to stop

    def test_nothing_that_deletes_anything_is_ever_run(self):
        forbidden = {"down", "rm", "prune", "-v", "--volumes", "kill"}
        for action in servers.ACTIONS:
            shell = FakeShell()
            shell.ps_sequence = [[row("db"), row("gameserver")]]
            servers.change({**SERVER, "file": "compose.profile.yml"}, action, shell, sleep=lambda s: None)
            for command, _ in shell.commands:
                self.assertFalse(forbidden & set(command.split()), command)
        with self.assertRaises(ValueError):
            servers.change(SERVER, "down", FakeShell())

    def test_docker_is_started_when_it_can_be_and_the_reason_is_said_when_it_cannot(self):
        said, say = self.say_into()
        shell = FakeShell(docker=False)
        ticks = iter(range(0, 1000, 5))
        self.assertTrue(servers.ensure_docker(shell, say, sleep=lambda s: None, clock=lambda: next(ticks), can_launch=True))
        self.assertIn("open -a Docker", [c for c, _ in shell.commands])
        said, say = self.say_into()
        self.assertFalse(servers.ensure_docker(FakeShell(docker=False), say, can_launch=False))
        self.assertIn("can't be started from here", said[0])
        result = servers.change(SERVER, "start", FakeShell(docker=False), sleep=lambda s: None)
        self.assertEqual((result["ok"], result["state"]), (False, "docker-off"))


class RemoteTests(unittest.TestCase):
    def test_another_computer_is_reached_over_ssh_in_its_copy_of_the_project(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")

        shell = servers.shell_for(Path("/p"), {"execution_mode": "ssh", "ssh_target": ["me@air.local"], "repo_path": "/Users/me/game"})
        self.assertIsInstance(shell, servers.SshShell)
        with patch.object(servers.subprocess, "run", side_effect=fake_run):
            self.assertEqual(shell.run("docker compose up -d", "srv/inner", 30), (0, "ok\n", False))
        self.assertEqual(calls[0][:5], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"])
        self.assertEqual(calls[0][5], "me@air.local")
        self.assertEqual(calls[0][6], "cd /Users/me/game/srv/inner && docker compose up -d")

    def test_this_computer_is_used_for_local_machines_and_incomplete_ssh_machines(self):
        for machine in (None, {"execution_mode": "local"}, {"execution_mode": "ssh", "ssh_target": "x"}, {"execution_mode": "ssh", "repo_path": "/x"}):
            self.assertIsInstance(servers.shell_for(Path("/p"), machine), servers.LocalShell)


if __name__ == "__main__":
    unittest.main()


class WebTests(unittest.TestCase):
    """The Server card's endpoints and actions."""

    def make(self):
        from test_web_ui import UI_HEADERS, ServerTestCase, ui

        class Case(ServerTestCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)
        write(case.root, "srv/docker-compose.yml", COMPOSE)
        return case, ui, UI_HEADERS

    def test_the_actions_build_safe_commands_and_only_the_owner_may_start_them(self):
        case, ui, headers = self.make()
        argv = ui.ACTIONS["server_start"].build({"id": "srv-default"}, case.root)
        self.assertEqual(argv[-3:], ["server", "start", "srv-default"])
        self.assertEqual(ui.ACTIONS["server_stop"].build({"id": "srv-default", "machine": "air"}, case.root)[-5:],
                         ["server", "stop", "srv-default", "--machine", "air"])
        self.assertNotIn("--machine", ui.ACTIONS["server_restart"].build({"id": "x", "machine": "local"}, case.root))
        for bad in ({"id": "a; rm -rf /"}, {"id": "ok", "machine": "x; reboot"}, {}):
            with self.assertRaises(ui.UIError):
                ui.ACTIONS["server_start"].build(bad, case.root)
        self.assertTrue({"server_start", "server_stop", "server_restart"} <= ui.OWNER_ONLY_ACTIONS)

    def test_the_card_gets_each_server_with_its_status_and_the_computers_it_can_be_managed_on(self):
        case, ui, headers = self.make()
        (case.root / ".orchestrator" / "config").mkdir(exist_ok=True)
        (case.root / ".orchestrator" / "config" / "machines.json").write_text(json.dumps({"machines": [
            {"name": "local", "execution_mode": "local"},
            {"name": "air", "execution_mode": "ssh", "ssh_target": "me@air", "repo_path": "/Users/me/game"},
            {"name": "no-path", "execution_mode": "ssh", "ssh_target": "x"}]}))
        shell = FakeShell()
        shell.ps_sequence = [[row("db", ports=[3307]), row("gameserver", "exited")]]
        with patch.object(servers, "shell_for", return_value=shell):
            res, data = case.request("GET", "/api/servers")
        self.assertEqual(res.status, 200)
        server = data["servers"][0]
        self.assertEqual((server["id"], server["dir"], server["status"]["state"]), ("srv-default", "srv", "partial"))
        self.assertEqual(server["ports"][:2], [3307, 10300])
        self.assertEqual([m["name"] for m in data["machines"]], ["local", "air"])  # an ssh machine without a project folder isn't offered
        self.assertTrue(data["docker"])
        self.assertNotIn("secret", json.dumps(data))

    def test_docker_being_off_is_reported_not_raised_and_an_unknown_computer_is_refused(self):
        case, ui, headers = self.make()
        with patch.object(servers, "shell_for", return_value=FakeShell(docker=False)):
            _, data = case.request("GET", "/api/servers")
        self.assertEqual((data["docker"], data["servers"][0]["status"]["state"]), (False, "docker-off"))
        self.assertEqual(case.request("GET", "/api/servers?machine=nope")[0].status, 400)

    def test_projects_without_compose_files_have_no_servers(self):
        from test_web_ui import ServerTestCase

        class Case(ServerTestCase):
            def runTest(self):
                pass

        case = Case()
        case.setUp()
        self.addCleanup(case.tearDown)
        self.assertEqual(case.request("GET", "/api/servers")[1]["servers"], [])

    def test_logs_come_from_the_named_server_only(self):
        case, ui, headers = self.make()
        shell = FakeShell({"logs --tail": (0, "game ready\n")})
        with patch.object(servers, "shell_for", return_value=shell):
            res, data = case.request("GET", "/api/servers/logs?id=srv-default")
            self.assertEqual((res.status, data["logs"]), (200, "game ready\n"))
            self.assertEqual(case.request("GET", "/api/servers/logs?id=../../etc")[0].status, 404)
        self.assertIn("docker compose logs --tail 80 --no-color", [c for c, _ in shell.commands])
