"""A project's product requirements document is the same on all of an account's computers.

It lived in `.orchestrator/product/prd.md` (git-ignored), so each computer had its own: the Air held only the seed copy, and a
job continued there was judged against different product context. These run two computers' syncs through the real control plane."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "cloud" / "functions"))
import control_plane as cp  # noqa: E402
from orchestrator import account, product_sync as ps  # noqa: E402
from orchestrator.prd import Prd, PrdChanged, replace_section, set_sources, split, template  # noqa: E402
from tests import test_control_plane as plane_tests  # noqa: E402
from tests.test_web_ui import ServerTestCase  # noqa: E402

REPO = "github.com/alice/shop"


def doc(**sections):
    text = template()
    for sid, body in sections.items():
        text = replace_section(text, sid, body)
    return text


class MergeTests(unittest.TestCase):
    BASE = doc(pitch="Sells shoes.", features="- Browse\n- Buy")

    def test_a_section_only_one_side_changed_takes_that_side(self):
        mine = doc(pitch="Sells shoes online.", features="- Browse\n- Buy")
        theirs = doc(pitch="Sells shoes.", features="- Browse\n- Buy\n- Track orders")
        text, conflicts = ps.merge(self.BASE, mine, theirs, "Laptop")
        self.assertEqual(conflicts, [])
        self.assertEqual(split(text)["pitch"], "Sells shoes online.")
        self.assertEqual(split(text)["features"], "- Browse\n- Buy\n- Track orders")

    def test_a_section_both_changed_keeps_both_texts_and_says_whose(self):
        mine = doc(pitch="Sells shoes online.")
        theirs = doc(pitch="Sells sneakers to runners.")
        text, conflicts = ps.merge(doc(pitch="Sells shoes."), mine, theirs, "Laptop")
        self.assertEqual(conflicts, ["Pitch"])
        pitch = split(text)["pitch"]
        self.assertIn("Sells shoes online.", pitch)
        self.assertIn("**Also written on Laptop:**", pitch)
        self.assertIn("Sells sneakers to runners.", pitch)
        self.assertLess(pitch.index("shoes online"), pitch.index("sneakers"))  # this computer's text first

    def test_the_marker_survives_a_reread_because_it_is_not_taken_for_guidance(self):
        text, _ = ps.merge(doc(pitch="a"), doc(pitch="b"), doc(pitch="c"), "Laptop")
        self.assertIn("**Also written on Laptop:**", split(text)["pitch"])

    def test_with_no_common_base_sections_fill_in_from_whichever_side_has_them(self):
        mine = doc(pitch="Sells shoes.")
        theirs = doc(who="Runners", pitch="Sells shoes.")
        text, conflicts = ps.merge(None, mine, theirs, "Laptop")
        self.assertEqual((conflicts, split(text)["who"], split(text)["pitch"]), ([], "Runners", "Sells shoes."))
        text, conflicts = ps.merge(None, doc(pitch="mine"), doc(pitch="theirs"), "Laptop")
        self.assertEqual(conflicts, ["Pitch"])  # both wrote something different and neither is a base

    def test_an_empty_side_never_wipes_or_marks_anything(self):
        base = doc(pitch="Sells shoes.")
        text, conflicts = ps.merge(base, doc(pitch="Sells shoes online."), doc(), "Laptop")  # they cleared it, we edited it
        self.assertEqual((conflicts, split(text)["pitch"]), ([], "Sells shoes online."))
        text, conflicts = ps.merge(base, doc(), doc(pitch="Sells sneakers."), "Laptop")      # we cleared it, they edited it
        self.assertEqual((conflicts, split(text)["pitch"]), ([], "Sells sneakers."))

    def test_the_shared_text_leaves_out_the_source_documents_list(self):
        with_sources = set_sources(doc(pitch="x"), [{"label": "Spec", "target": "source/spec.md"}])
        self.assertIn("Source documents", with_sources)
        self.assertNotIn("Source documents", ps.shareable(with_sources))
        self.assertEqual(ps.shareable(ps.shareable(with_sources)), ps.shareable(with_sources))  # stable, so hashes agree
        self.assertFalse(ps.has_content(template()))
        self.assertTrue(ps.has_content(doc(pitch="x")))


class TwoComputers(unittest.TestCase):
    """Two computers of one account, each with its own project folder, talking to one control plane."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = plane_tests.Clock()
        self.plane = cp.ControlPlane(cp.MemoryStore(), now=self.clock)
        case = plane_tests.PlaneCase("setUp")
        case.plane = self.plane
        case.assertEqual = self.assertEqual
        self.studio = self.pair(case, "Studio")
        self.laptop = self.pair(case, "Laptop")
        self.calls = []
        self.root = {}
        for name in ("studio", "laptop"):
            root = Path(self.tmp.name) / name
            (root / ".orchestrator").mkdir(parents=True)
            self.root[name] = root

    @staticmethod
    def pair(case, name):
        machine_id, secret = case.pair(name=name)
        return {"machine_id": machine_id, "machine_secret": secret, "name": name}

    def call(self, path, body=None, machine=None):
        self.calls.append(path)
        headers = {"authorization": f"Machine {machine['machine_id']}:{machine['machine_secret']}"} if machine else {}
        status, reply = self.plane.handle("POST", f"/cp/{path}", headers, body or {}, plane_tests.verify_user)
        if status != 200:
            raise account.AccountError(reply.get("error", "error"), status)
        return reply

    def prd(self, name):
        return Prd(self.root[name], self.root[name] / ".orchestrator")

    def write(self, name, text, source="you"):
        self.prd(name).write(text, source)

    def sync(self, name, call=None):
        return ps.sync_project(getattr(self, name), self.root[name], REPO, call=call or self.call)

    def section(self, name, sid):
        return split(self.prd(name).read() or "").get(sid, "")


class SyncTests(TwoComputers):
    def test_the_first_computer_pushes_and_the_other_pulls_it_as_a_visible_undoable_change(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.assertEqual(self.sync("studio"), "pushed")
        self.assertEqual(self.sync("laptop"), "pulled")
        self.assertEqual(self.section("laptop", "pitch"), "Sells shoes.")
        newest = self.prd("laptop").history()[0]
        self.assertEqual((newest["source"], newest["summary"]), ("sync", "Synced from Studio"))
        self.assertEqual(self.prd("laptop").notice()["summary"], "Synced from Studio")  # announced, never silent
        self.assertIsNone(self.prd("studio").notice())                                  # the sender had nothing arrive

    def test_after_both_hold_the_same_document_rounds_do_nothing_and_ask_for_nothing_more(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.sync("laptop")
        settled = len(self.calls)
        for name in ("studio", "laptop", "studio", "laptop"):
            self.assertEqual(self.sync(name), "idle")
        self.assertEqual(self.calls[settled:], ["machine/product"] * 4)  # one look at the account each, no writes

    def test_an_edit_here_goes_to_the_other_computer(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.sync("laptop")
        self.write("laptop", doc(pitch="Sells shoes.", who="Runners"))
        self.assertEqual(self.sync("laptop"), "pushed")
        self.assertEqual(self.sync("studio"), "pulled")
        self.assertEqual(self.section("studio", "who"), "Runners")

    def test_edits_to_different_sections_on_both_merge_with_no_conflict(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.sync("laptop")
        self.write("studio", doc(pitch="Sells shoes online."))
        self.write("laptop", doc(pitch="Sells shoes.", features="- Browse"))
        self.assertEqual(self.sync("studio"), "pushed")
        self.assertEqual(self.sync("laptop"), "merged")
        self.assertEqual(self.prd("laptop").notice()["summary"], "Merged with Studio's version")
        self.assertEqual((self.section("laptop", "pitch"), self.section("laptop", "features")), ("Sells shoes online.", "- Browse"))
        self.assertEqual(self.sync("studio"), "pulled")
        for sid in ("pitch", "features"):
            self.assertEqual(self.section("studio", sid), self.section("laptop", sid))

    def test_the_same_section_edited_on_both_keeps_both_and_both_computers_end_up_with_it(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.sync("laptop")
        self.write("studio", doc(pitch="Sells shoes online."))
        self.write("laptop", doc(pitch="Sells sneakers to runners."))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "merged")
        summary = self.prd("laptop").notice()["summary"]
        self.assertEqual(summary, "Merged with Studio's version; both kept in Pitch")
        pitch = self.section("laptop", "pitch")
        self.assertTrue(all(part in pitch for part in ("Sells sneakers to runners.", "Also written on Studio", "Sells shoes online.")))
        self.assertEqual(self.sync("studio"), "pulled")
        self.assertEqual(self.section("studio", "pitch"), pitch)  # nothing lost anywhere
        self.assertEqual((self.sync("studio"), self.sync("laptop")), ("idle", "idle"))

    def test_a_different_document_on_a_computer_that_never_synced_is_merged_not_replaced(self):
        self.write("studio", doc(pitch="Sells shoes.", who="Runners"))
        self.sync("studio")
        self.write("laptop", doc(pitch="Sells shoes.", features="- Browse"))  # written on its own before ever syncing
        self.assertEqual(self.sync("laptop"), "merged")
        self.assertEqual((self.section("laptop", "who"), self.section("laptop", "features")), ("Runners", "- Browse"))

    def test_an_empty_template_is_never_pushed_and_never_replaces_a_real_document(self):
        self.assertEqual(self.sync("laptop"), "idle")  # nothing anywhere
        self.assertNotIn("machine/product/put", self.calls)
        self.write("laptop", template())
        self.assertEqual(self.sync("laptop"), "idle")
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "pulled")  # the blank one takes the real one, not the other way round
        self.assertEqual(self.sync("studio"), "idle")
        self.assertEqual(self.section("studio", "pitch"), "Sells shoes.")

    def test_a_computer_still_holding_the_untouched_seed_takes_the_evolved_document_instead_of_merging_with_it(self):
        seed = doc(pitch="The first idea.", who="Anyone", features="- One thing")
        (self.root["laptop"] / "docs" / "product").mkdir(parents=True)
        (self.root["laptop"] / "docs" / "product" / "prd.md").write_text(seed)  # tracked in the repo, as the seed is
        self.prd("laptop").read()  # first read copies the seed into the shared runtime, verbatim
        self.assertEqual(self.prd("laptop").read(), seed)
        self.write("studio", doc(pitch="What it became.", who="Runners", features="- Two things"))
        self.sync("studio")
        self.assertEqual(self.sync("laptop"), "pulled")  # not "merged": no "Also written on" copies of the old seed text
        self.assertEqual(self.section("laptop", "pitch"), "What it became.")
        self.assertNotIn("Also written", self.prd("laptop").read())

    def test_an_untouched_seed_is_never_pushed_but_an_edited_one_is(self):
        seed = doc(pitch="The first idea.")
        (self.root["laptop"] / "docs" / "product").mkdir(parents=True)
        (self.root["laptop"] / "docs" / "product" / "prd.md").write_text(seed)
        self.prd("laptop").read()
        self.assertEqual(self.sync("laptop"), "idle")
        self.assertNotIn("machine/product/put", self.calls)
        self.write("laptop", doc(pitch="The first idea, refined."))
        self.assertEqual(self.sync("laptop"), "pushed")

    def test_each_computer_keeps_its_own_source_documents_list(self):
        self.write("studio", set_sources(doc(pitch="Sells shoes."), [{"label": "Studio spec", "target": "source/studio.md"}]))
        self.sync("studio")
        self.write("laptop", set_sources(doc(who="Runners"), [{"label": "Laptop notes", "target": "source/laptop.md"}]))
        self.sync("laptop")
        laptop = self.prd("laptop").read()
        self.assertIn("Laptop notes", laptop)
        self.assertNotIn("Studio spec", laptop)
        self.assertNotIn("Source documents", self.plane.store.get("account_products", next(iter(self.plane.store.data["account_products"])))["text"])

    def test_a_push_that_loses_a_race_starts_again_from_what_won(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        self.sync("laptop")
        self.write("laptop", doc(pitch="Sells shoes.", who="Runners"))
        raced = []

        def call(path, body=None, machine=None):
            if path == "machine/product/put" and not raced:   # the Studio pushes first, in the middle of the laptop's round
                raced.append(True)
                self.call(path, {"project": REPO, "text": doc(pitch="Sells shoes.", features="- Browse"), "base_version": 1}, self.studio)
            return self.call(path, body, machine)

        self.assertEqual(self.sync("laptop", call=call), "merged")
        self.assertEqual((self.section("laptop", "who"), self.section("laptop", "features")), ("Runners", "- Browse"))
        self.assertEqual(self.section("studio", "pitch"), "Sells shoes.")

    def test_an_edit_made_during_a_round_is_never_overwritten(self):
        self.write("studio", doc(pitch="Sells shoes."))
        self.sync("studio")
        prd = self.prd("laptop")
        with patch.object(Prd, "write_if_current", side_effect=PrdChanged("edited meanwhile")):
            self.assertEqual(self.sync("laptop"), "busy")
        self.assertFalse(ps.has_content(prd.read()))          # nothing was written by the round that backed off
        self.assertEqual(self.sync("laptop"), "pulled")        # the next round simply tries again

    def test_write_if_current_refuses_when_the_document_moved_on(self):
        prd = self.prd("studio")
        prd.write(doc(pitch="one"), "you")
        before = prd.read()
        prd.write(doc(pitch="two"), "you")
        with self.assertRaises(PrdChanged):
            prd.write_if_current(before, doc(pitch="three"), "sync")
        self.assertEqual(split(prd.read())["pitch"], "two")
        self.assertIsNotNone(prd.write_if_current(prd.read(), doc(pitch="three"), "sync"))


class FailureTests(TwoComputers):
    def test_a_control_plane_without_the_endpoints_is_reported_so_the_caller_can_wait(self):
        def old_plane(path, body=None, machine=None):
            raise account.AccountError("Not found", 404)
        self.write("studio", doc(pitch="x"))
        with self.assertRaises(ps.Unavailable):
            self.sync("studio", call=old_plane)

    def test_being_offline_leaves_everything_as_it_was(self):
        self.write("studio", doc(pitch="x"))

        def offline(path, body=None, machine=None):
            raise account.AccountError("Couldn't reach Orchestrator's servers (timed out).")
        with self.assertRaises(account.AccountError):
            self.sync("studio", call=offline)
        self.assertEqual(self.section("studio", "pitch"), "x")
        self.assertEqual(self.sync("studio"), "pushed")  # and it just works once back online

    def test_sync_all_reports_each_project_and_one_failure_does_not_stop_the_rest(self):
        good, bad = self.root["studio"], self.root["laptop"]
        self.write("studio", doc(pitch="x"))
        original = ps.sync_project

        def flaky(machine, root, repo, *, call):
            if root == bad:
                raise RuntimeError("unreadable file")
            return original(machine, root, repo, call=call)

        with patch.object(ps, "project_roots", return_value=[(bad, "github.com/alice/blog"), (good, REPO)]), patch.object(ps, "sync_project", flaky):
            result = ps.sync_all(self.studio, call=self.call)
        self.assertEqual(result, {"github.com/alice/blog": "error: unreadable file", REPO: "pushed"})

    def test_sync_all_lets_unavailable_through(self):
        with patch.object(ps, "project_roots", return_value=[(self.root["studio"], REPO)]), \
                patch.object(ps, "sync_project", side_effect=ps.Unavailable("Not found")):
            with self.assertRaises(ps.Unavailable):
                ps.sync_all(self.studio, call=self.call)


class ScheduledRoundTests(unittest.TestCase):
    def test_a_round_records_how_it_went_and_says_when_to_come_back(self):
        machine = {"machine_id": "m", "machine_secret": "s"}
        with patch.object(ps.sync_health, "record") as record:
            with patch.object(ps, "sync_all", return_value={"a": "pushed", "b": "idle"}):
                self.assertEqual(ps.run_round(machine, None), ps.CHECK_SECONDS)
            record.assert_called_with("product", True, "")
            with patch.object(ps, "sync_all", return_value={"a": "pushed", "b": "error: Couldn't reach Orchestrator's servers (timed out)."}):
                self.assertEqual(ps.run_round(machine, None), ps.CHECK_SECONDS)
            record.assert_called_with("product", False, "Couldn't reach Orchestrator's servers (timed out).")
            with patch.object(ps, "sync_all", side_effect=RuntimeError("disk full")):
                self.assertEqual(ps.run_round(machine, None), ps.CHECK_SECONDS)
            record.assert_called_with("product", False, "disk full")

    def test_a_control_plane_without_this_feature_is_left_alone_for_an_hour_and_not_reported_as_a_problem(self):
        with patch.object(ps.sync_health, "record") as record, patch.object(ps, "sync_all", side_effect=ps.Unavailable("Not found")):
            self.assertEqual(ps.run_round({"machine_id": "m"}, None), ps.UNAVAILABLE_WAIT)
        record.assert_not_called()

    def test_the_channel_is_known_to_sync_health_so_a_lasting_failure_shows_on_home(self):
        from orchestrator import sync_health
        self.assertIn("product", sync_health.CHANNELS)


class HeartbeatWiringTests(ServerTestCase):
    def test_a_paired_computer_runs_the_product_sync_thread(self):
        rounds = []

        def run_round(machine, root, **kwargs):
            rounds.append((machine["machine_id"], root))
            self.server._stopping.set()
            return 0.01

        with patch.object(account, "load_machine", return_value={"machine_id": "one"}), patch.object(account, "heartbeat", return_value={}), \
                patch.object(account, "sync_snapshot"), patch.object(ps, "CHECK_SECONDS", 0.01), patch.object(ps, "run_round", side_effect=run_round):
            thread = self.server.start_heartbeat(lambda: "https://x.example.com", interval=.01)
            thread.join(3)
            deadline = time.time() + 3
            while not rounds and time.time() < deadline:  # its own thread, started beside the heartbeat's
                time.sleep(0.02)
        self.assertEqual(rounds, [("one", self.root)])


class ProjectListTests(unittest.TestCase):
    def test_projects_without_a_repository_are_skipped_and_a_projects_worktrees_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, none = (Path(tmp) / n for n in ("a", "b", "none"))
            for p in (a, b, none):
                (p / ".orchestrator").mkdir(parents=True)
            recent = {"projects": [{"root": str(a), "github_repo": "alice/shop"}, {"root": str(b), "github_repo": "alice/blog"},
                                   {"root": str(none)}, {"root": str(Path(tmp) / "gone")}]}
            repos = {str(a): "alice/shop", str(b): "alice/blog", str(none): ""}
            with patch("orchestrator.project_config.load_recent_projects", return_value=recent), \
                    patch.object(account, "project_repo", side_effect=lambda path, gh=None: repos.get(str(path), "")):
                found = ps.project_roots(a)
            self.assertEqual([(str(p), r) for p, r in found], [(str(a), "alice/shop"), (str(b), "alice/blog")])


if __name__ == "__main__":
    unittest.main()
