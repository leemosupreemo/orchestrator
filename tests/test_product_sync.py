"""A project's product requirements document is the same on all the computers working on it, through the project's own
repository (refs/orchestrator/state), not Orchestrator's servers.

It lived in `.orchestrator/product/prd.md` (git-ignored), so each computer had its own: the Air held only the seed copy, and a
job continued there was judged against different product context. These run two computers against a real bare remote."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator import product_sync as ps, state_ref  # noqa: E402
from orchestrator.prd import Prd, PrdChanged, replace_section, set_sources, sources, split, template  # noqa: E402
from tests.test_web_ui import UI_HEADERS, ServerTestCase  # noqa: E402


def doc(**sections):
    text = template()
    for sid, body in sections.items():
        text = replace_section(text, sid, body)
    return text


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class MergeTests(unittest.TestCase):
    BASE = doc(pitch="Sells shoes.", features="- Browse\n- Buy")

    def test_a_section_only_one_side_changed_takes_that_side(self):
        mine = doc(pitch="Sells shoes online.", features="- Browse\n- Buy")
        theirs = doc(pitch="Sells shoes.", features="- Browse\n- Buy\n- Track orders")
        text, conflicts = ps.merge(self.BASE, mine, theirs, "Laptop")
        self.assertEqual(conflicts, [])
        self.assertEqual((split(text)["pitch"], split(text)["features"]), ("Sells shoes online.", "- Browse\n- Buy\n- Track orders"))

    def test_a_section_both_changed_keeps_both_texts_and_says_whose(self):
        text, conflicts = ps.merge(doc(pitch="Sells shoes."), doc(pitch="Sells shoes online."), doc(pitch="Sells sneakers to runners."), "Laptop")
        self.assertEqual(conflicts, ["Pitch"])
        pitch = split(text)["pitch"]
        self.assertIn("**Also written on Laptop:**", pitch)
        self.assertLess(pitch.index("shoes online"), pitch.index("sneakers"))  # this computer's text first, nothing dropped

    def test_with_no_common_base_sections_fill_in_from_whichever_side_has_them(self):
        text, conflicts = ps.merge(None, doc(pitch="Sells shoes."), doc(who="Runners", pitch="Sells shoes."), "Laptop")
        self.assertEqual((conflicts, split(text)["who"]), ([], "Runners"))

    def test_an_empty_side_never_wipes_or_marks_anything(self):
        base = doc(pitch="Sells shoes.")
        self.assertEqual(ps.merge(base, doc(pitch="Sells shoes online."), doc(), "L"), (ps.merge(base, doc(pitch="Sells shoes online."), doc(), "L")[0], []))
        self.assertEqual(split(ps.merge(base, doc(), doc(pitch="Sells sneakers."), "L")[0])["pitch"], "Sells sneakers.")

    def test_source_lists_are_joined(self):
        mine = set_sources(doc(pitch="x"), [{"label": "Spec", "target": "source/spec.md"}])
        theirs = set_sources(doc(pitch="x"), [{"label": "Notes", "target": "source/notes.md"}])
        text, _ = ps.merge(doc(pitch="x"), mine, theirs, "Laptop")
        self.assertEqual([s["target"] for s in sources(text)], ["source/spec.md", "source/notes.md"])


class TwoComputers(unittest.TestCase):
    """Two computers, each with its own clone of one project, sharing a bare remote."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote = base / "remote.git"
        git(base, "init", "-q", "--bare", str(self.remote))
        self.root = {}
        for name in ("studio", "laptop"):
            root = base / name
            root.mkdir()
            git(root, "init", "-q", "-b", "main")
            git(root, "remote", "add", "origin", str(self.remote))
            (root / ".orchestrator").mkdir()
            state_ref.set_consent(root, True)
            self.root[name] = root

    def prd(self, name):
        return Prd(self.root[name], self.root[name] / ".orchestrator")

    def write(self, name, text):
        self.prd(name).write(text, "you")

    def sync(self, name):
        return ps.sync_project(self.root[name], name=name.capitalize())

    def section(self, name, sid):
        return split(self.prd(name).read() or "").get(sid, "")

    def both_synced(self, text):
        self.write("studio", text)
        self.assertEqual(self.sync("studio"), "pushed")
        self.assertEqual(self.sync("laptop"), "pulled")


class SyncTests(TwoComputers):
    def test_the_first_computer_pushes_and_the_other_pulls_it_as_a_visible_undoable_change(self):
        self.both_synced(doc(pitch="Sells shoes."))
        self.assertEqual(self.section("laptop", "pitch"), "Sells shoes.")
        newest = self.prd("laptop").history()[0]
        self.assertEqual((newest["source"], newest["summary"]), ("sync", "Synced from Studio"))
        self.assertEqual(self.prd("laptop").notice()["summary"], "Synced from Studio")  # announced, never silent
        self.assertIsNone(self.prd("studio").notice())

    def test_once_both_agree_rounds_are_idle(self):
        self.both_synced(doc(pitch="Sells shoes."))
        before = git(self.remote, "rev-parse", state_ref.REF)
        for name in ("studio", "laptop", "studio", "laptop"):
            self.assertEqual(self.sync(name), "idle")
        self.assertEqual(git(self.remote, "rev-parse", state_ref.REF), before)  # nothing more was pushed

    def test_an_edit_here_goes_to_the_other_computer(self):
        self.both_synced(doc(pitch="Sells shoes."))
        self.write("laptop", doc(pitch="Sells shoes.", who="Runners"))
        self.assertEqual(self.sync("laptop"), "pushed")
        self.assertEqual(self.sync("studio"), "pulled")
        self.assertEqual(self.section("studio", "who"), "Runners")

    def test_edits_to_different_sections_on_both_merge_and_converge(self):
        self.both_synced(doc(pitch="Sells shoes."))
        self.write("studio", doc(pitch="Sells shoes online."))
        self.write("laptop", doc(pitch="Sells shoes.", features="- Browse"))
        self.assertEqual(self.sync("studio"), "pushed")
        self.assertEqual(self.sync("laptop"), "merged")
        self.assertEqual(self.prd("laptop").notice()["summary"], "Merged with Studio's version")
        self.assertEqual(self.sync("studio"), "pulled")
        self.assertEqual(self.prd("studio").read(), self.prd("laptop").read())
        self.assertEqual((self.sync("studio"), self.sync("laptop")), ("idle", "idle"))

    def test_the_same_section_edited_on_both_keeps_both_everywhere(self):
        self.both_synced(doc(pitch="Sells shoes."))
        self.write("studio", doc(pitch="Sells shoes online."))
        self.write("laptop", doc(pitch="Sells sneakers to runners."))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "merged")
        self.assertEqual(self.prd("laptop").notice()["summary"], "Merged with Studio's version; both kept in Pitch")
        self.sync("studio")
        pitch = self.section("studio", "pitch")
        self.assertTrue(all(p in pitch for p in ("Sells sneakers to runners.", "Also written on Studio", "Sells shoes online.")))

    def test_a_computer_still_holding_the_untouched_seed_takes_the_evolved_document(self):
        seed = doc(pitch="The first idea.", who="Anyone")
        (self.root["laptop"] / "docs" / "product").mkdir(parents=True)
        (self.root["laptop"] / "docs" / "product" / "prd.md").write_text(seed)
        self.assertEqual(self.prd("laptop").read(), seed)  # copied in on first read, verbatim
        self.assertEqual(self.sync("laptop"), "idle")       # the seed alone is never pushed
        self.write("studio", doc(pitch="What it became.", who="Runners"))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "pulled")     # not merged: no "Also written" copies of the old seed
        self.assertNotIn("Also written", self.prd("laptop").read())

    def test_an_empty_template_is_never_pushed_over_a_real_document(self):
        self.write("laptop", template())
        self.assertEqual(self.sync("laptop"), "idle")
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", state_ref.REF], cwd=self.remote).returncode, 1)
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "pulled")
        self.assertEqual(self.section("studio", "pitch"), "Sells shoes.")

    def test_a_push_that_loses_a_race_starts_again_from_what_won(self):
        self.both_synced(doc(pitch="Sells shoes."))
        self.write("laptop", doc(pitch="Sells shoes.", who="Runners"))
        original, raced = state_ref.push, []

        def push(repo, sha):
            if Path(repo) == self.root["laptop"] and not raced:   # the studio gets in first, mid-round
                raced.append(True)
                self.write("studio", doc(pitch="Sells shoes.", features="- Browse"))
                self.assertEqual(ps.sync_project(self.root["studio"], name="Studio"), "pushed")
            return original(repo, sha)

        with patch.object(state_ref, "push", side_effect=push):
            self.assertEqual(self.sync("laptop"), "merged")
        self.assertEqual((self.section("laptop", "who"), self.section("laptop", "features")), ("Runners", "- Browse"))
        self.assertEqual(self.sync("studio"), "pulled")
        self.assertEqual(self.section("studio", "who"), "Runners")  # the loser's edit wasn't lost either

    def test_an_edit_made_during_a_round_is_never_overwritten(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        with patch.object(Prd, "write_if_current", side_effect=PrdChanged("edited meanwhile")):
            self.assertEqual(self.sync("laptop"), "busy")
        self.assertFalse(ps.has_content(self.prd("laptop").read()))
        self.assertEqual(self.sync("laptop"), "pulled")


class FilesTests(TwoComputers):
    def test_design_and_source_files_travel_and_are_never_overwritten(self):
        studio_designs = self.prd("studio").path.parent / "designs"
        studio_designs.mkdir(parents=True)
        (studio_designs / "home.png").write_bytes(b"\x89PNG studio")
        self.both_synced(doc(look="- [Home](designs/home.png)"))
        self.assertEqual((self.prd("laptop").path.parent / "designs" / "home.png").read_bytes(), b"\x89PNG studio")
        laptop_source = self.prd("laptop").path.parent / "source"
        laptop_source.mkdir(parents=True)
        (laptop_source / "spec.md").write_text("laptop spec")
        (self.prd("laptop").path.parent / "designs" / "home.png").write_bytes(b"\x89PNG laptop edit")  # same name, changed here
        self.assertEqual(self.sync("laptop"), "files")
        self.sync("studio")
        self.assertEqual((self.prd("studio").path.parent / "source" / "spec.md").read_text(), "laptop spec")
        self.assertEqual((studio_designs / "home.png").read_bytes(), b"\x89PNG studio")  # a union: never overwritten

    def test_the_source_documents_list_travels_with_the_files(self):
        self.write("studio", set_sources(doc(pitch="x"), [{"label": "Spec", "target": "source/spec.md"}]))
        self.sync("studio")
        self.write("laptop", set_sources(doc(pitch="x"), [{"label": "Notes", "target": "source/notes.md"}]))
        self.sync("laptop")
        self.sync("studio")
        for name in ("studio", "laptop"):
            self.assertEqual(sorted(s["target"] for s in sources(self.prd(name).read())), ["source/notes.md", "source/spec.md"])


class RepositoryTests(TwoComputers):
    def test_nothing_is_shared_until_the_person_says_yes_for_the_project(self):
        state_ref.set_consent(self.root["studio"], False)
        self.write("studio", doc(pitch="Private plans."))
        self.assertEqual(self.sync("studio"), "off")
        (self.root["laptop"] / ".orchestrator" / "state" / "repo_sync.json").unlink()
        self.assertIsNone(state_ref.consent(self.root["laptop"]))
        self.assertEqual(self.sync("laptop"), "off")  # not asked yet is not a yes
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", state_ref.REF], cwd=self.remote).returncode, 1)

    def test_a_project_without_a_remote_stays_on_its_computer(self):
        git(self.root["studio"], "remote", "remove", "origin")
        self.write("studio", doc(pitch="x"))
        self.assertEqual(self.sync("studio"), "no remote")
        self.assertEqual(state_ref.status(self.root["studio"])["state"], "no_remote")

    def test_the_repositorys_pre_push_hook_does_not_run_or_block_it(self):
        hook = self.root["studio"] / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\ntouch hook-ran\nexit 1\n")
        hook.chmod(0o755)
        self.write("studio", doc(pitch="x"))
        self.assertEqual(self.sync("studio"), "pushed")
        self.assertFalse((self.root["studio"] / "hook-ran").exists())

    def test_state_commits_carry_no_personal_identity_and_name_the_computer(self):
        self.write("studio", doc(pitch="x"))
        self.sync("studio")
        author = git(self.remote, "log", "-1", "--format=%an <%ae> / %cn <%ce>", state_ref.REF)
        self.assertEqual(author, "Orchestrator <orchestrator@localhost> / Orchestrator <orchestrator@localhost>")
        self.assertEqual(state_ref.computer(self.root["studio"], git(self.root["studio"], "rev-parse", state_ref.TRACKING_REF)), "Studio")

    def test_an_ordinary_clone_does_not_fetch_the_state_and_branches_are_untouched(self):
        self.write("studio", doc(pitch="x"))
        self.sync("studio")
        clone = Path(self.tmp.name) / "fresh"
        git(self.tmp.name, "clone", "-q", str(self.remote), str(clone))
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", "-q", state_ref.REF], cwd=clone).returncode, 1)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)"), state_ref.REF)  # no branch was created

    def test_an_unreachable_remote_raises_and_changes_nothing_here(self):
        self.write("studio", doc(pitch="x"))
        git(self.root["studio"], "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"))
        with self.assertRaises(state_ref.StateRefError):
            self.sync("studio")
        self.assertEqual(self.section("studio", "pitch"), "x")

    def test_the_first_versions_single_answer_still_counts_for_the_product_requirements_only(self):
        path = self.root["studio"] / ".orchestrator" / "state" / "repo_sync.json"
        path.write_text('{"enabled": true, "at": 1}')
        self.assertEqual((state_ref.consent(self.root["studio"]), state_ref.consent(self.root["studio"], "jobs")), (True, None))
        state_ref.set_consent(self.root["studio"], True, "jobs")
        self.assertEqual((state_ref.consent(self.root["studio"]), state_ref.consent(self.root["studio"], "jobs")), (True, True))

    def test_status_says_what_the_product_page_should_ask(self):
        status = state_ref.status(self.root["studio"])
        self.assertEqual((status["state"], status["public"]), ("on", None))  # a local path: visibility unknown
        state_ref.set_consent(self.root["studio"], False)
        self.assertEqual(state_ref.status(self.root["studio"])["state"], "off")

    def test_github_visibility_comes_from_gh_and_unknown_hosts_say_so(self):
        state_ref._visibility_cache.clear()
        public = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="PUBLIC\n", stderr="")
        private = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="PRIVATE\n", stderr="")
        self.assertTrue(state_ref.is_public(self.root["studio"], "git@github.com:alice/shop.git", run=public))
        state_ref._visibility_cache.clear()
        self.assertFalse(state_ref.is_public(self.root["studio"], "https://github.com/alice/shop", run=private))
        self.assertIsNone(state_ref.is_public(self.root["studio"], "https://gitlab.com/alice/shop.git", run=public))


class RoundTests(unittest.TestCase):
    def test_a_round_records_problems_only_for_projects_that_share(self):
        with patch.object(ps.sync_health, "record") as record:
            with patch.object(ps, "sync_all", return_value={"a": "off", "b": "no remote"}):
                self.assertEqual(ps.run_round(None), ps.CHECK_SECONDS)
            record.assert_not_called()
            with patch.object(ps, "sync_all", return_value={"a": "pushed", "b": "error: Permission denied (publickey)."}):
                ps.run_round(None)
            record.assert_called_with("product", False, "Permission denied (publickey).")
            with patch.object(ps, "sync_all", return_value={"a": "idle"}):
                ps.run_round(None)
            record.assert_called_with("product", True, "")

    def test_one_projects_failure_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            with patch.object(ps, "project_roots", return_value=[a, b]), \
                    patch.object(ps, "sync_project", side_effect=[state_ref.StateRefError("offline"), "pushed"]):
                self.assertEqual(ps.sync_all(), {str(a): "error: offline", str(b): "pushed"})

    def test_projects_are_listed_once_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            for p in (a, b):
                (p / ".orchestrator").mkdir(parents=True)
            recent = {"projects": [{"root": str(a)}, {"root": str(b)}, {"root": str(a)}, {"root": str(Path(tmp) / "gone")}]}
            with patch("orchestrator.project_config.load_recent_projects", return_value=recent):
                self.assertEqual(ps.project_roots(a), [a, b])


class ProductPageSharingTests(ServerTestCase):
    def test_the_product_page_says_whether_it_is_shared_and_the_answer_is_saved(self):
        _, data = self.request("GET", "/api/product")
        self.assertIn(data["sharing"]["state"], ("no_remote", "ask"))
        subprocess.run(["git", "init", "-q"], cwd=self.root, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", "https://gitlab.example.com/team/shop.git"], cwd=self.root, capture_output=True)
        _, data = self.request("GET", "/api/product")
        self.assertEqual(data["sharing"], {"state": "ask", "jobs": "ask", "remote": "gitlab.example.com/team/shop", "public": None})
        res, status = self.request("POST", "/api/product/sharing", body={"enabled": True}, headers=UI_HEADERS)
        self.assertEqual((res.status, status["state"]), (200, "on"))
        self.assertTrue(state_ref.consent(self.root))
        res, status = self.request("POST", "/api/product/sharing", body={"enabled": False}, headers=UI_HEADERS)
        self.assertEqual(status["state"], "off")
        self.assertEqual(self.request("POST", "/api/product/sharing", body={"enabled": "yes"}, headers=UI_HEADERS)[0].status, 400)
        res, status = self.request("POST", "/api/product/sharing", body={"enabled": True, "scope": "jobs"}, headers=UI_HEADERS)
        self.assertEqual((status["state"], status["jobs"]), ("off", "on"))  # each kind of state has its own answer
        self.assertEqual(self.request("POST", "/api/product/sharing", body={"enabled": True, "scope": "logs"}, headers=UI_HEADERS)[0].status, 400)

    def test_the_page_asks_once_warns_on_public_repositories_and_offers_a_way_back(self):
        app = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text()
        page = app[app.index("const sharingAsk = () =>"):app.index("const render = () =>", app.index("const sharingAsk = () =>"))]
        self.assertIn("Share with your other computers?", page)
        self.assertIn("Anyone who can read the repository can read them.", page)
        self.assertIn("This repository is public, so anyone could read them.", page)
        public_buttons = page[page.index("s.public\n          ?"):page.index(":", page.index("s.public\n          ?") + 20)]
        self.assertIn('class="btn small primary" data-prd-share="false">Keep on this computer', public_buttons)  # the safe choice leads
        for line in ("Stop sharing", "Share through", "this project has no Git remote", 'href="#/git"'):
            self.assertIn(line, page)


class ServerWiringTests(ServerTestCase):
    def test_the_server_runs_the_sync_whether_or_not_the_computer_is_paired(self):
        rounds = []

        def run_round(root):
            rounds.append(root)
            self.server._stopping.set()
            return 0.01

        with patch.object(ps, "CHECK_SECONDS", 0.01), patch.object(ps, "run_round", side_effect=run_round):
            thread = self.server.start_state_sync()
            thread.join(3)
        self.assertEqual(rounds, [self.root])
        self.assertEqual(thread.name, "product-sync")


if __name__ == "__main__":
    unittest.main()
