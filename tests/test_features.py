from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import features as F  # noqa: E402


def job(jid, feature, group="working"):
    return {"id": jid, "feature": feature, "state": {"group": group}}


class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_persists_and_dedupes_ids(self):
        a = F.create(self.rt, "Lobby seats", "who sits where", "Sources/Lobby/\n./Sources/Lobby/\nTests/Lobby")
        b = F.create(self.rt, "Lobby Seats")
        self.assertEqual(a["id"], "lobby-seats")
        self.assertEqual(b["id"], "lobby-seats-2")
        self.assertEqual(a["paths"], ["Sources/Lobby/", "Tests/Lobby"])
        self.assertEqual([f["id"] for f in F.load(self.rt)], ["lobby-seats", "lobby-seats-2"])

    def test_name_is_required_and_bounded(self):
        for bad in ("", "   ", "x" * 81):
            with self.assertRaises(F.FeatureError):
                F.create(self.rt, bad)

    def test_update_and_delete(self):
        f = F.create(self.rt, "Chat")
        F.update(self.rt, f["id"], name="Chat v2", paths=["Sources/Chat"])
        self.assertEqual(F.load(self.rt)[0]["name"], "Chat v2")
        with self.assertRaises(F.FeatureError):
            F.update(self.rt, f["id"], name=" ")
        F.delete(self.rt, f["id"])
        self.assertEqual(F.load(self.rt), [])
        with self.assertRaises(F.FeatureError):
            F.delete(self.rt, f["id"])

    def test_load_tolerates_a_missing_or_corrupt_file(self):
        self.assertEqual(F.load(self.rt), [])
        F.store_path(self.rt).write_text("{nope")
        self.assertEqual(F.load(self.rt), [])


class FeatureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.fid = F.create(self.rt, "Chat")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def status(self):
        return F.load(self.rt)[0]

    def test_attaching_open_work_starts_a_planned_feature(self):
        F.note_work_attached(self.rt, self.fid, "working")
        self.assertEqual(self.status()["status"], "in-progress")

    def test_complete_is_not_final_new_work_reopens_it(self):
        F.set_status(self.rt, self.fid, "complete")
        self.assertIsNotNone(self.status()["completed_at"])
        F.note_work_attached(self.rt, self.fid, "needs_you")
        self.assertEqual((self.status()["status"], self.status()["reopened"]), ("in-progress", True))
        F.set_status(self.rt, self.fid, "complete")
        self.assertFalse(self.status()["reopened"])

    def test_attaching_finished_work_changes_nothing(self):
        F.set_status(self.rt, self.fid, "complete")
        F.note_work_attached(self.rt, self.fid, "done")
        self.assertEqual(self.status()["status"], "complete")

    def test_invalid_status_rejected(self):
        with self.assertRaises(F.FeatureError):
            F.set_status(self.rt, self.fid, "shipped")


class RollupAndOverlapTests(unittest.TestCase):
    def test_rollup_counts_jobs_by_state_group(self):
        feats = [{"id": "a", "name": "A", "paths": []}, {"id": "b", "name": "B", "paths": []}]
        jobs = [job("1", "a", "done"), job("2", "a", "working"), job("3", "a", "needs_you"), job("4", None)]
        rolled = {f["id"]: f for f in F.rollup(feats, jobs)}
        self.assertEqual((rolled["a"]["jobs_total"], rolled["a"]["jobs_done"], rolled["a"]["jobs_working"], rolled["a"]["jobs_need_you"]), (3, 1, 1, 1))
        self.assertEqual(rolled["b"]["jobs_total"], 0)

    def test_overlapping_and_nested_paths_are_flagged(self):
        feats = [{"id": "a", "name": "A", "paths": ["Sources/Lobby/"]},
                 {"id": "b", "name": "B", "paths": ["Sources/Lobby/Seat.swift"]},
                 {"id": "c", "name": "C", "paths": ["Sources/Chat/"]}]
        out = F.overlaps(feats, [])
        self.assertEqual([o["features"] for o in out], [["a", "b"]])

    def test_sibling_paths_do_not_overlap(self):
        feats = [{"id": "a", "name": "A", "paths": ["Sources/Lobby"]}, {"id": "b", "name": "B", "paths": ["Sources/LobbyChat"]}]
        self.assertEqual(F.overlaps(feats, []), [])

    def test_in_flight_jobs_touching_one_file_are_flagged_but_finished_ones_are_not(self):
        feats = [{"id": "a", "name": "A", "paths": []}, {"id": "b", "name": "B", "paths": []}]
        files = {"1": ["App.swift"], "2": ["App.swift"], "3": ["App.swift"]}
        out = F.overlaps(feats, [job("1", "a"), job("2", "b")], files)
        self.assertEqual(out[0]["reasons"], ["in-flight jobs both change App.swift"])
        self.assertEqual(F.overlaps(feats, [job("1", "a"), job("3", "b", "done")], files), [])

    def test_same_feature_never_overlaps_itself(self):
        feats = [{"id": "a", "name": "A", "paths": []}]
        self.assertEqual(F.overlaps(feats, [job("1", "a"), job("2", "a")], {"1": ["x"], "2": ["x"]}), [])


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.auth = F.create(self.rt, "Auth")["id"]
        self.lobby = F.create(self.rt, "Lobby", depends_on=[self.auth])["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def rolled(self):
        return {f["id"]: f for f in F.rollup(F.load(self.rt), [])}

    def test_layers_follow_the_deepest_dependency(self):
        chat = F.create(self.rt, "Chat", depends_on=[self.auth, self.lobby])["id"]
        r = self.rolled()
        self.assertEqual((r[self.auth]["layer"], r[self.lobby]["layer"], r[chat]["layer"]), (0, 1, 2))

    def test_unknown_self_and_circular_dependencies_are_rejected(self):
        with self.assertRaises(F.FeatureError):
            F.create(self.rt, "X", depends_on=["nope"])
        with self.assertRaises(F.FeatureError):
            F.update(self.rt, self.auth, depends_on=[self.auth])
        with self.assertRaises(F.FeatureError):
            F.update(self.rt, self.auth, depends_on=[self.lobby])  # lobby already needs auth
        self.assertEqual(self.rolled()[self.auth]["depends_on"], [])

    def test_waiting_on_lists_unfinished_dependencies_until_complete(self):
        self.assertEqual(self.rolled()[self.lobby]["waiting_on"], [self.auth])
        F.set_status(self.rt, self.auth, "complete")
        self.assertEqual(self.rolled()[self.lobby]["waiting_on"], [])
        F.set_status(self.rt, self.lobby, "complete")
        F.set_status(self.rt, self.auth, "in-progress")
        self.assertEqual(self.rolled()[self.lobby]["waiting_on"], [])  # a finished feature isn't "waiting"

    def test_deleting_a_feature_removes_it_as_a_dependency(self):
        F.delete(self.rt, self.auth)
        self.assertEqual(self.rolled()[self.lobby]["depends_on"], [])
        self.assertEqual(self.rolled()[self.lobby]["layer"], 0)

    def test_hand_edited_cycles_do_not_hang_layout(self):
        feats = [{"id": "x", "depends_on": ["y"]}, {"id": "y", "depends_on": ["x"]}]
        self.assertEqual(set(F.layers(feats)), {"x", "y"})


class KpiStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.fid = F.create(self.rt, "Lobby")["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def kpis(self):
        return {f["id"]: f for f in F.rollup(F.load(self.rt), [])}[self.fid]["kpis"]

    def test_add_measure_update_delete_round_trip(self):
        k = F.kpi_add(self.rt, self.fid, {"name": "Seat claims", "event": "seat_claimed", "target": 60})
        F.kpi_measure(self.rt, self.fid, k["id"], 70, "after launch", "keep")
        row = self.kpis()[0]
        self.assertEqual((row["status"]["state"], row["status"]["latest"]["decision"]), ("on-track", "keep"))
        F.kpi_update(self.rt, self.fid, k["id"], {"target": 90})
        self.assertEqual((self.kpis()[0]["status"]["state"], len(self.kpis()[0]["measurements"])), ("behind", 1))
        F.kpi_delete(self.rt, self.fid, k["id"])
        self.assertEqual(self.kpis(), [])

    def test_errors_surface_as_feature_errors_and_save_nothing(self):
        with self.assertRaises(F.FeatureError):
            F.kpi_add(self.rt, self.fid, {"name": "x", "event": "Bad Name"})
        with self.assertRaises(F.FeatureError):
            F.kpi_measure(self.rt, self.fid, "ghost", 1)
        with self.assertRaises(F.FeatureError):
            F.kpi_add(self.rt, "nope", {"name": "x", "event": "ok_event"})
        self.assertEqual(self.kpis(), [])


class ServesTests(unittest.TestCase):
    def test_a_feature_can_say_which_use_case_it_serves_and_edit_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = Path(tmp)
            f = F.create(rt, "Rematch", serves="  As a player I can ask for a rematch  ")
            self.assertEqual(f["serves"], "As a player I can ask for a rematch")
            F.update(rt, f["id"], serves="As a player I can keep playing")
            self.assertEqual(F.load(rt)[0]["serves"], "As a player I can keep playing")
            F.update(rt, f["id"], serves="x" * 500)
            self.assertEqual(len(F.load(rt)[0]["serves"]), 300)
            self.assertEqual(F.restore(rt, {**F.load(rt)[0], "id": "copy"})["serves"], "x" * 300)


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = Path(self.tmp.name)
        self.auth = F.create(self.rt, "Auth")["id"]
        self.lobby = F.create(self.rt, "Lobby", "Seats", "src/lobby/", [self.auth])["id"]
        k = F.kpi_add(self.rt, self.lobby, {"name": "Claims", "event": "seat_claimed", "target": 60})
        F.kpi_measure(self.rt, self.lobby, k["id"], 70, "wk1", "keep")
        F.set_status(self.rt, self.lobby, "complete")

    def tearDown(self):
        self.tmp.cleanup()

    def record(self):
        return next(f for f in F.load(self.rt) if f["id"] == self.lobby)

    def test_a_deleted_feature_comes_back_whole(self):
        before = self.record()
        F.delete(self.rt, self.lobby)
        restored = F.restore(self.rt, before)
        after = self.record()
        for key in ("id", "name", "summary", "paths", "depends_on", "status", "completed_at"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["kpis"][0]["measurements"][0]["decision"], "keep")
        self.assertEqual(restored["id"], self.lobby)

    def test_restore_rejects_clashes_bad_ids_and_junk_without_trusting_input(self):
        with self.assertRaises(F.FeatureError):
            F.restore(self.rt, self.record())  # already exists
        with self.assertRaises(F.FeatureError):
            F.restore(self.rt, {"id": "../etc", "name": "x"})
        with self.assertRaises(F.FeatureError):
            F.restore(self.rt, {"id": "ok", "name": " "})
        with self.assertRaises(F.FeatureError):
            F.restore(self.rt, "nope")
        out = F.restore(self.rt, {"id": "new-one", "name": "New", "status": "shipped", "depends_on": ["ghost", self.auth, "new-one"],
                                  "kpis": [{"name": "bad", "event": "Bad Event"}, {"name": "ok", "event": "ok_event", "measurements": [{"t": 1, "value": "x"}, {"t": 2, "value": 3}]}],
                                  "evil": "x"})
        self.assertEqual((out["status"], out["depends_on"], len(out["kpis"]), len(out["kpis"][0]["measurements"])), ("planned", [self.auth], 1, 1))
        self.assertNotIn("evil", out)

    def test_a_deleted_kpi_comes_back_with_its_results(self):
        kpi = self.record()["kpis"][0]
        F.kpi_delete(self.rt, self.lobby, kpi["id"])
        F.kpi_restore(self.rt, self.lobby, kpi)
        back = self.record()["kpis"][0]
        self.assertEqual((back["id"], len(back["measurements"])), (kpi["id"], 1))
        with self.assertRaises(F.FeatureError):
            F.kpi_restore(self.rt, self.lobby, kpi)


if __name__ == "__main__":
    unittest.main()
