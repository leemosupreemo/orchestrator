from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import project_health as H  # noqa: E402

HEALTHY = {"prd_sections": {"pitch": True, "who": True, "features": True, "look": False, "not": False}, "prd_auto_update": True, "agents": True, "readme": True, "platforms": ["iOS app"], "recommend_pending": False, "detected": "Swift (Xcode)",
           "git_repo": True, "remote": True, "tag": "v1.0.0", "unreleased": 2, "jobs_total": 5, "jobs_open": 1, "jobs_unassigned": 0,
           "features": 3, "suites": 4, "cases_total": 6, "cases_gap": 0, "kpis_total": 2, "kpis_measured": 2, "features_needing_kpis": 0,
           "ci": True, "pipeline_failing": False, "distribution": True, "builds_sent": 3}
EMPTY = {k: (False if isinstance(v, bool) else 0 if isinstance(v, int) else None if v is None or isinstance(v, str) else []) for k, v in HEALTHY.items()}
EMPTY.update({"tag": None, "unreleased": None, "detected": "", "recommend_pending": False, "prd_sections": {}, "prd_auto_update": True})


def by_id(result):
    return {i["id"]: i for i in result["items"]}


class EvaluateTests(unittest.TestCase):
    def test_a_healthy_project_has_nothing_to_do(self):
        r = H.evaluate(HEALTHY)
        self.assertEqual((r["ok"], r["total"], r["next"], r["stage"]), (10, 10, None, "Released (changes pending)"))

    def test_an_empty_project_starts_with_the_product_requirements(self):
        r = H.evaluate(EMPTY)
        self.assertEqual((r["stage"], r["next"]), ("Getting started", "prd"))
        prd_item = by_id(r)["prd"]
        self.assertEqual((prd_item["status"], prd_item["route"]), ("todo", "#/product"))
        self.assertIn("import a PRD", prd_item["detail"])
        self.assertNotIn("review", by_id(r))  # the product review and its reminder are gone: the document keeps itself true

    def test_every_open_item_says_how_to_fix_it(self):
        for item in H.evaluate(EMPTY)["items"]:
            if item["status"] == "ok":
                continue
            self.assertTrue(item.get("job") or item.get("route") or item.get("hint"), item["id"])

    def test_undecided_platforms_are_flagged_not_missing(self):
        item = by_id(H.evaluate({**HEALTHY, "platforms": [], "recommend_pending": True}))["platforms"]
        self.assertEqual(item["status"], "warn")
        self.assertEqual(item["job"]["type"], "feature")

    def test_platforms_fall_back_to_the_detected_stack(self):
        item = by_id(H.evaluate({**HEALTHY, "platforms": []}))["platforms"]
        self.assertEqual((item["status"], item["detail"]), ("ok", "Detected: Swift (Xcode)"))
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "platforms": [], "detected": ""}))["platforms"]["status"], "todo")

    def test_test_states(self):
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "suites": 0}))["tests"]["status"], "todo")
        gap = by_id(H.evaluate({**HEALTHY, "cases_gap": 2}))["tests"]
        self.assertEqual((gap["status"], gap["route"]), ("warn", "#/tests?cases=unassigned"))

    def test_repo_states(self):
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "remote": False}))["repo"]["hint"], "gh repo create --source . --push")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "git_repo": False, "remote": False}))["repo"]["hint"], "git init")

    def test_ci_delivery_and_release_states(self):
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "pipeline_failing": True}))["ci"]["status"], "warn")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "ci": False}))["ci"]["status"], "todo")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "distribution": False}))["delivery"]["route"], "#/config/firebase")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "builds_sent": 0}))["delivery"]["route"], "#/delivery")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "tag": None}))["release"]["status"], "todo")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "unreleased": H.UNRELEASED_WARN + 1}))["release"]["status"], "warn")

    def test_feature_and_kpi_states(self):
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "features": 0}))["features"]["status"], "todo")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "jobs_unassigned": 2}))["features"]["status"], "warn")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "features_needing_kpis": 1}))["kpis"]["status"], "todo")
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "kpis_measured": 0}))["kpis"]["status"], "warn")

    def test_the_product_requirements_item_follows_what_is_written(self):
        written = lambda **kw: by_id(H.evaluate({**HEALTHY, "prd_sections": {"pitch": False, "who": False, "features": False, "look": False, "not": False, **kw}}))["prd"]
        self.assertEqual(written()["status"], "todo")
        part = written(pitch=True, who=True)
        self.assertEqual((part["status"], part["route"]), ("todo", "#/product"))
        self.assertIn("core features", part["detail"])
        self.assertNotIn("who it's for", part["detail"])
        done = written(pitch=True, who=True, features=True, look=True)
        self.assertEqual(done["status"], "ok")
        self.assertIn("4 of 5", done["detail"])
        self.assertIn("updates itself", done["detail"])

    def test_it_says_when_automatic_updates_are_off(self):
        item = by_id(H.evaluate({**HEALTHY, "prd_auto_update": False}))["prd"]
        self.assertEqual(item["status"], "ok")
        self.assertIn("Automatic updates are off", item["detail"])

    def test_next_prefers_a_missing_thing_over_a_warning(self):
        r = H.evaluate({**HEALTHY, "cases_gap": 1, "ci": False})
        self.assertEqual(r["next"], "ci")

    def test_stage_progression(self):
        base = {**HEALTHY, "tag": None, "unreleased": None}
        self.assertEqual(H.evaluate({**base, "builds_sent": 0})["stage"], "Building")
        self.assertEqual(H.evaluate(base)["stage"], "With testers")
        self.assertEqual(H.evaluate({**HEALTHY, "unreleased": 0})["stage"], "Released")


if __name__ == "__main__":
    unittest.main()
