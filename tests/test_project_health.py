from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import project_health as H  # noqa: E402

HEALTHY = {"brief": True, "agents": True, "readme": True, "platforms": ["iOS app"], "recommend_pending": False, "detected": "Swift (Xcode)",
           "git_repo": True, "remote": True, "tag": "v1.0.0", "unreleased": 2, "jobs_total": 5, "jobs_open": 1, "jobs_unassigned": 0,
           "features": 3, "suites": 4, "cases_total": 6, "cases_gap": 0, "kpis_total": 2, "kpis_measured": 2, "features_needing_kpis": 0,
           "ci": True, "pipeline_failing": False, "distribution": True, "builds_sent": 3,
           "docs": [{"id": i, "title": t, "filled": True, "exists": True} for i, t in (("brief", "Product brief"), ("use-cases", "Users and non-goals"), ("journey", "User journey"),
                                                                                    ("screens", "Screens"), ("architecture", "Technical decisions"), ("plan", "Current plan"))],
           "review": {"path": "docs/product/reviews/x.md", "days": 3, "stale": False}}
EMPTY = {k: (False if isinstance(v, bool) else 0 if isinstance(v, int) else None if v is None or isinstance(v, str) else []) for k, v in HEALTHY.items()}
EMPTY.update({"tag": None, "unreleased": None, "detected": "", "recommend_pending": False, "docs": [], "review": None})


def by_id(result):
    return {i["id"]: i for i in result["items"]}


class EvaluateTests(unittest.TestCase):
    def test_a_healthy_project_has_nothing_to_do(self):
        r = H.evaluate(HEALTHY)
        self.assertEqual((r["ok"], r["total"], r["next"], r["stage"]), (12, 12, None, "Released (changes pending)"))

    def test_an_empty_project_starts_with_the_brief(self):
        r = H.evaluate(EMPTY)
        self.assertEqual((r["stage"], r["next"]), ("Getting started", "brief"))
        self.assertEqual([i["id"] for i in r["items"] if i["status"] == "ok"], ["review"])  # nothing to review yet is not a to-do
        brief = by_id(r)["brief"]
        self.assertEqual(brief["job"]["type"], "quick")
        self.assertIn("product-brief.md", brief["job"]["summary"])

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

    def test_unfilled_product_documents_are_called_out_with_a_fix(self):
        docs = [dict(d, filled=d["id"] == "brief") for d in HEALTHY["docs"]]
        item = by_id(H.evaluate({**HEALTHY, "docs": docs}))["product-docs"]
        self.assertEqual((item["status"], item["route"]), ("todo", "#/product"))
        self.assertIn("5 of 5", item["detail"])
        self.assertIn("users and non-goals", item["detail"])
        one = [dict(d, filled=d["id"] != "plan") for d in HEALTHY["docs"]]
        self.assertIn("1 of 5", by_id(H.evaluate({**HEALTHY, "docs": one}))["product-docs"]["detail"])

    def test_no_document_listing_means_no_documents_item(self):
        self.assertNotIn("product-docs", by_id(H.evaluate({**HEALTHY, "docs": []})))

    def test_reviews_are_only_nagged_for_once_there_is_enough_to_review(self):
        stale = {"path": "p", "days": 30, "stale": True}
        self.assertEqual(by_id(H.evaluate({**HEALTHY, "review": None, "jobs_total": 2}))["review"]["status"], "ok")
        never = by_id(H.evaluate({**HEALTHY, "review": None, "jobs_total": H.REVIEW_AFTER_JOBS}))["review"]
        self.assertEqual((never["status"], never["route"]), ("warn", "#/product"))
        self.assertIn("No review yet", never["detail"])
        old = by_id(H.evaluate({**HEALTHY, "review": stale}))["review"]
        self.assertEqual(old["status"], "warn")
        self.assertIn("30 days ago", old["detail"])

    def test_next_prefers_a_missing_thing_over_a_warning(self):
        r = H.evaluate({**HEALTHY, "cases_gap": 1, "ci": False})
        self.assertEqual(r["next"], "ci")

    def test_stage_progression(self):
        base = {**HEALTHY, "tag": None, "unreleased": None}
        self.assertEqual(H.evaluate({**base, "builds_sent": 0})["stage"], "Building")
        self.assertEqual(H.evaluate(base)["stage"], "With testers")
        self.assertEqual(H.evaluate({**HEALTHY, "unreleased": 0})["stage"], "Released")


class BriefParseTests(unittest.TestCase):
    def test_reads_bulleted_platforms_and_the_undecided_marker(self):
        text = "# X\n\n## Platforms\n\n- iOS app\n- Web app\n\n## Out of scope\n"
        self.assertEqual(H.brief_platforms(text), (["iOS app", "Web app"], False))
        undecided = "## Platforms\n\n_Not decided._ The AI should recommend.\n\n## Next\n"
        self.assertEqual(H.brief_platforms(undecided), ([], True))

    def test_old_singular_heading_and_no_section(self):
        self.assertEqual(H.brief_platforms("## Platform\n\n- iOS app\n"), (["iOS app"], False))
        self.assertEqual(H.brief_platforms("# nothing here"), ([], False))


if __name__ == "__main__":
    unittest.main()
