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


if __name__ == "__main__":
    unittest.main()
