"""Tests for loose MECE coordination across parallel jobs (orchestrator/parallel_mece.py)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "orchestrator" / "scripts"))

from orchestrator import parallel_jobs, parallel_mece, scope_check  # noqa: E402
from orchestrator.web import server as ui  # noqa: E402


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


class PeerLaneTests(unittest.TestCase):
    def test_active_peers_prioritizes_running_and_same_group_and_filters_inactive(self):
        current = {"job_id": "cur", "feature": "auth", "parent_job_id": "p1"}
        jobs = [
            ("cur", {"title": "Current", "status": "executing", "plan": {"likely_files": ["src/cur.py"]}}),
            ("done", {"title": "Completed", "status": "completed", "plan": {"likely_files": ["src/done.py"]}}),
            ("arch", {"title": "Archived", "status": "planned", "archived": True, "plan": {"likely_files": ["src/a.py"]}}),
            ("open_other", {"title": "Other Open", "status": "planned", "updated_at": "2026-11-18T10:00:00Z",
                            "plan": {"likely_files": ["src/other.py"]}}),
            ("open_sibling", {"title": "Sibling Open", "status": "planned", "parent_job_id": "p1",
                              "updated_at": "2026-11-18T09:00:00Z", "plan": {"likely_files": ["src/sib.py"]}}),
            ("running_job", {"title": "Active Runner", "status": "executing", "updated_at": "2026-11-18T08:00:00Z",
                             "plan": {"summary": "Updates session middleware", "likely_files": ["src/auth/session.py"]}}),
        ]
        peers = parallel_mece.active_peers(jobs, current_job_id="cur", current_job=current)
        self.assertEqual([p["id"] for p in peers], ["running_job", "open_sibling", "open_other"])
        self.assertTrue(peers[0]["running"])
        self.assertTrue(peers[1]["same_group"])

    def test_lane_context_is_empty_without_peers_and_role_specific_with_peers(self):
        self.assertEqual(parallel_mece.lane_context([], role="planner"), "")
        self.assertEqual(parallel_mece.lane_context([], role="builder"), "")

        peers = [{
            "id": "job-1",
            "title": "Add OAuth",
            "status": "executing",
            "running": True,
            "summary": "Adds OAuth login flow",
            "files": [f"src/f{i}.py" for i in range(10)],
        }]
        planner_block = parallel_mece.lane_context(peers, role="planner")
        self.assertIn("ACTIVE PARALLEL WORK (SOFT MECE LANES)", planner_block)
        self.assertIn("Owned files:", planner_block)
        self.assertIn("+2 more", planner_block)
        self.assertIn("Keep this plan mutually exclusive", planner_block)

        builder_block = parallel_mece.lane_context(peers, role="builder")
        self.assertIn("Hands-off files:", builder_block)
        self.assertIn("strictly additive and minimal", builder_block)

    def test_load_active_peers_ignores_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            jdir = Path(tmp)
            (jdir / "bad.json").write_text("{not valid json", encoding="utf-8")
            (jdir / "good.json").write_text(json.dumps({
                "title": "Good Peer",
                "status": "executing",
                "plan": {"likely_files": ["src/a.py"]},
            }), encoding="utf-8")
            peers = parallel_mece.load_active_peers(jdir, current_job_id="me")
            self.assertEqual([p["id"] for p in peers], ["good"])

    def test_run_builder_injects_active_peer_lanes_into_prompt(self):
        import run_builder

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_dir = root / ".orchestrator" / "jobs"
            out_dir = root / ".orchestrator" / "output"
            jobs_dir.mkdir(parents=True)
            out_dir.mkdir(parents=True)
            my_job = {
                "job_id": "job-a",
                "type": "bug-fix",
                "title": "Fix button spacing",
                "status": "executing",
                "plan": {"summary": "Adjust padding", "likely_files": ["src/ui/button.py"]},
            }
            peer_job = {
                "job_id": "job-b",
                "type": "feature-plan",
                "title": "OAuth Login",
                "status": "executing",
                "plan": {"summary": "Add OAuth callback", "likely_files": ["src/auth/oauth.py"]},
            }
            my_path = jobs_dir / "job-a.json"
            my_path.write_text(json.dumps(my_job), encoding="utf-8")
            (jobs_dir / "job-b.json").write_text(json.dumps(peer_job), encoding="utf-8")

            captured_prompts: list[str] = []

            def fake_run_llm(model, prompt, **kwargs):
                captured_prompts.append(prompt)
                return '{"summary": "Done"}', "mock-model", "sid-1"

            fake_stack = mock.Mock(display_name="Python", language="python", build_command="", test_command="pytest")
            fake_cfg = mock.Mock(stack=fake_stack, build_command="", test_command="pytest", test_target="tests")
            with mock.patch.object(run_builder, "ROOT", root), \
                 mock.patch.object(run_builder, "OUTPUT_DIR", out_dir), \
                 mock.patch.object(run_builder, "PROJECT_CONFIG", fake_cfg), \
                 mock.patch.object(run_builder, "run_llm", side_effect=fake_run_llm), \
                 mock.patch.object(run_builder, "run_build_and_tests", return_value=(True, True)), \
                 mock.patch.object(run_builder, "resolve_builder_model", return_value="mock-model"), \
                 mock.patch("orchestrator.prd.context_block", return_value=""), \
                 mock.patch("orchestrator.architecture.context_block", return_value=""), \
                 mock.patch("orchestrator.builder_check.tree_fingerprint", return_value={}):
                run_builder.run_builder(my_path, resume=False)

            self.assertEqual(len(captured_prompts), 1)
            self.assertIn("ACTIVE PARALLEL WORK (SOFT MECE LANES)", captured_prompts[0])
            self.assertIn("OAuth Login", captured_prompts[0])
            self.assertIn("src/auth/oauth.py", captured_prompts[0])


class TieredLaunchOverlapTests(unittest.TestCase):
    def test_hard_wait_on_exact_file_overlap(self):
        running = [("a", {"title": "Grass", "status": "executing", "plan": {"likely_files": ["World/Grass.cs"]}})]
        res = parallel_mece.classify_launch_overlap("b", {"title": "Shaders", "plan": {"likely_files": ["World/Grass.cs"]}}, running)
        self.assertEqual(res["tier"], "hard")
        self.assertIn("same files as “Grass”", res["wait_reason"])

    def test_hard_wait_for_unplanned_job_only_when_colliding_with_blind_or_related_peer(self):
        blind_peer = [("a", {"title": "Quick cleanup", "status": "executing", "plan": {}})]
        res_blind = parallel_mece.classify_launch_overlap("b", {"title": "Another quick fix", "plan": {}}, blind_peer)
        self.assertEqual(res_blind["tier"], "hard")
        self.assertIn("without a file list", res_blind["wait_reason"])

        same_feat_peer = [("a", {"title": "Auth API", "feature": "auth", "plan": {"likely_files": ["src/auth/api.py"]}})]
        res_feat = parallel_mece.classify_launch_overlap("b", {"title": "Auth tweak", "feature": "auth", "plan": {}}, same_feat_peer)
        self.assertEqual(res_feat["tier"], "hard")
        self.assertIn("same work as “Auth API”", res_feat["wait_reason"])

        similar_peer = [("a", {"title": "Login page OAuth button", "plan": {"likely_files": ["web/login.js"]}})]
        res_sim = parallel_mece.classify_launch_overlap("b", {"title": "Login page OAuth styling", "plan": {}}, similar_peer)
        self.assertEqual(res_sim["tier"], "hard")
        self.assertIn("closely related", res_sim["wait_reason"])

        unrelated_planned_peer = [("a", {"title": "Terrain grass", "plan": {"likely_files": ["World/Grass.cs"]}})]
        res_ok = parallel_mece.classify_launch_overlap("b", {"title": "Settings button spacing", "plan": {}}, unrelated_planned_peer)
        self.assertEqual(res_ok["tier"], "disjoint")

    def test_soft_overlap_allows_parallel_launch_with_advisory(self):
        running = [("a", {"title": "Login flow", "feature": "auth", "status": "executing",
                          "plan": {"likely_files": ["src/auth/login.py"]}})]
        job = {"branch_mode": "new", "title": "Session store", "feature": "auth",
               "plan": {"likely_files": ["src/auth/session.py"]}}
        res = parallel_mece.classify_launch_overlap("b", job, running)
        self.assertEqual(res["tier"], "soft")
        self.assertEqual(res["wait_reason"], "")
        self.assertEqual(len(res["advisories"]), 1)
        self.assertIn("src/auth", res["advisories"][0]["shared_dirs"])
        self.assertTrue(res["advisories"][0]["same_group"])

        # parallel_jobs.decide allows soft-overlapping jobs to run in parallel
        use, why = parallel_jobs.decide(Path("/p"), "b", job, running, {"job": "a", "title": "Login flow"}, at_once=2)
        self.assertEqual((use, why), (True, ""))


class ScopePeerDriftTests(unittest.TestCase):
    def test_scope_check_flags_peer_overlap_and_respects_accepted(self):
        changed = [{"path": "src/a.py", "status": "M", "added": 10, "deleted": 2},
                   {"path": "src/shared.py", "status": "M", "added": 4, "deleted": 1}]
        r = scope_check.evaluate(
            ["src/a.py", "src/shared.py"],
            1,
            changed,
            peer_files={"src/shared.py": ["Other Active Job"]},
        )
        kinds = [f["kind"] for f in r["findings"]]
        self.assertEqual(kinds, ["peer_overlap"])
        self.assertEqual(r["findings"][0]["files"], ["src/shared.py"])
        self.assertIn("Other Active Job", r["findings"][0]["detail"])

        # Accepting the file clears the advisory
        r_acc = scope_check.evaluate(
            ["src/a.py", "src/shared.py"],
            1,
            changed,
            accepted=["src/shared.py"],
            peer_files={"src/shared.py": ["Other Active Job"]},
        )
        self.assertTrue(r_acc["clear"])

    def test_peer_file_map_ignores_completed_and_self(self):
        others = [
            ("me", {"title": "Me", "status": "executing", "plan": {"likely_files": ["src/me.py"]}}),
            ("done", {"title": "Done", "status": "completed", "plan": {"likely_files": ["src/done.py"]}}),
            ("peer", {"title": "Peer", "status": "executing", "plan": {"likely_files": ["src/peer.py"]},
                      "ai_modified_files": ["src/drifted.py"]}),
        ]
        m = parallel_mece.peer_file_map("me", {"status": "review-needed"}, others)
        self.assertEqual(m, {"src/drifted.py": ["Peer"], "src/peer.py": ["Peer"]})


class SiblingCoverageTests(unittest.TestCase):
    def test_clean_mece_split_with_deferred_is_ok(self):
        reqs = [
            "Users can sign in with email and password",
            "Sessions expire cleanly after timeout",
            "Admin audit log exports CSV",
        ]
        siblings = [
            ("s1", {"title": "Email sign-in", "status": "completed",
                    "plan": {"acceptance_criteria": ["Users can sign in with email and password"]}}),
            ("s2", {"title": "Session expiry", "status": "executing",
                    "plan": {"acceptance_criteria": ["Sessions expire cleanly after timeout"],
                             "deferred": ["Admin audit log exports CSV"]}}),
        ]
        cov = parallel_mece.evaluate_sibling_coverage(reqs, siblings, scope_kind="feature", scope_id="auth")
        self.assertTrue(cov["mece_ok"])
        self.assertEqual(cov["gaps"], [])
        self.assertEqual(cov["overlapping"], [])
        self.assertEqual(len(cov["covered"]), 2)
        self.assertEqual(len(cov["deferred"]), 1)
        self.assertEqual(cov["summary"], "")

    def test_detects_gaps_duplicate_claims_and_ignores_discarded_siblings(self):
        reqs = [
            "Support OAuth login redirect callback",
            "Refresh expired session tokens automatically",
            "Rate limit failed sign-in attempts",
        ]
        siblings = [
            ("s1", {"title": "OAuth callback", "status": "executing",
                    "plan": {"acceptance_criteria": ["Support OAuth login redirect callback"]}}),
            ("s2", {"title": "OAuth callback hardening", "status": "planned",
                    "plan": {"acceptance_criteria": ["Support OAuth login redirect callback"]}}),
            ("s3", {"title": "Discarded rate limiter", "status": "discarded",
                    "plan": {"acceptance_criteria": ["Rate limit failed sign-in attempts"]}}),
        ]
        cov = parallel_mece.evaluate_sibling_coverage(reqs, siblings, scope_kind="parent_job", scope_id="p1")
        self.assertFalse(cov["mece_ok"])
        self.assertEqual(len(cov["overlapping"]), 1)
        self.assertEqual(cov["overlapping"][0]["requirement"], "Support OAuth login redirect callback")
        self.assertEqual(cov["gaps"], [
            "Refresh expired session tokens automatically",
            "Rate limit failed sign-in attempts",
        ])
        self.assertIn("2 requirements not covered", cov["summary"])
        self.assertIn("1 requirement claimed by multiple sibling jobs", cov["summary"])

    def test_splinter_job_records_mece_coverage_on_parent(self):
        import splinter_job

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs_dir = root / ".orchestrator" / "jobs"
            out_dir = root / ".orchestrator" / "output"
            jobs_dir.mkdir(parents=True)
            out_dir.mkdir(parents=True)
            parent_file = jobs_dir / "parent-1.json"
            parent_job = {
                "job_id": "parent-1",
                "type": "feature-plan",
                "title": "Auth Rollout",
                "status": "approved",
                "plan": {
                    "acceptance_criteria": [
                        "Support OAuth login redirect callback",
                        "Document security rotation playbook",
                    ],
                    "tasks": [
                        {
                            "title": "OAuth login",
                            "description": "Implement OAuth callback handler",
                            "acceptance_criteria": ["Support OAuth login redirect callback"],
                            "likely_files": ["src/auth/oauth.py"],
                            "tests": ["tests/test_oauth.py"],
                            "complexity": "small",
                        }
                    ],
                },
            }
            parent_file.write_text(json.dumps(parent_job), encoding="utf-8")

            def fake_make_paths(jid: str):
                o = out_dir / jid
                o.mkdir(parents=True, exist_ok=True)
                return mock.Mock(job_file=jobs_dir / f"{jid}.json", brief_file=o / "brief.md")

            fake_cfg = mock.Mock(uses_github=False)
            with mock.patch.object(splinter_job, "PROJECT_CONFIG", fake_cfg), \
                 mock.patch.object(splinter_job, "make_job_paths", side_effect=fake_make_paths), \
                 mock.patch("model_router.resolve_builder_model", return_value="mock-model"):
                sub_ids = splinter_job.splinter_job(parent_file)

            self.assertEqual(len(sub_ids), 1)
            saved_parent = json.loads(parent_file.read_text(encoding="utf-8"))
            cov = saved_parent.get("mece_coverage")
            self.assertIsNotNone(cov)
            self.assertFalse(cov["mece_ok"])
            self.assertEqual(cov["gaps"], ["Document security rotation playbook"])


class ServerAndUiIntegrationTests(unittest.TestCase):
    def test_server_job_scope_and_detail_expose_peer_drift_and_mece_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "-b", "main")
            git(root, "config", "user.email", "test@example.invalid")
            git(root, "config", "user.name", "Test")
            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
            (root / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "init")

            git(root, "checkout", "-q", "-b", "ai/job-1")
            (root / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
            (root / "src" / "b.py").write_text("b = 2\n", encoding="utf-8")
            git(root, "add", ".")
            git(root, "commit", "-q", "-m", "work")
            git(root, "checkout", "-q", "main")

            runtime = root / ".orchestrator"
            (runtime / "jobs").mkdir(parents=True)
            (runtime / "project.json").write_text(json.dumps({"base_branch": "main"}), encoding="utf-8")
            (runtime / "features.json").write_text(json.dumps({
                "version": 1,
                "features": [{
                    "id": "auth",
                    "name": "Authentication",
                    "paths": ["src/"],
                    "requirements": [
                        "Users can sign in with email",
                        "Audit log records failed logins",
                    ],
                }],
            }), encoding="utf-8")
            (runtime / "jobs" / "job-1.json").write_text(json.dumps({
                "job_id": "job-1",
                "title": "Email Sign-In",
                "status": "review-needed",
                "feature": "auth",
                "branch": "ai/job-1",
                "base_branch": "main",
                "plan": {
                    "likely_files": ["src/a.py", "src/b.py"],
                    "acceptance_criteria": ["Users can sign in with email"],
                    "tasks": [{"title": "Email login", "likely_files": ["src/a.py"]}],
                },
            }), encoding="utf-8")
            (runtime / "jobs" / "job-2.json").write_text(json.dumps({
                "job_id": "job-2",
                "title": "Peer Using B",
                "status": "executing",
                "feature": "auth",
                "plan": {"likely_files": ["src/b.py"]},
            }), encoding="utf-8")

            scope = ui.job_scope(root, json.loads((runtime / "jobs" / "job-1.json").read_text(encoding="utf-8")))
            self.assertIsNotNone(scope)
            self.assertIn("peer_overlap", [f["kind"] for f in scope["findings"]])

            detail = ui.job_detail(root, "job-1")
            self.assertIsNotNone(detail["mece"])
            self.assertFalse(detail["mece"]["coverage"]["mece_ok"])
            self.assertEqual(detail["mece"]["coverage"]["gaps"], ["Audit log records failed logins"])

            overview = ui.features_overview(root)
            auth_feat = overview["features"][0]
            self.assertEqual(auth_feat["mece"]["gaps"], ["Audit log records failed logins"])

    def test_mece_coverage_html_renders_only_when_gaps_or_overlap_exist(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        app_js = (ROOT / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        start = app_js.index("function meceCoverageHtml(")
        end = app_js.index("\nfunction jobChangeSummary(", start)
        fn_src = app_js[start:end]
        script = f"""
const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
{fn_src}
const empty1 = meceCoverageHtml(null);
const empty2 = meceCoverageHtml({{ coverage: {{ mece_ok: true, summary: "" }} }});
const shown = meceCoverageHtml({{
  coverage: {{
    mece_ok: false,
    summary: "1 requirement not covered by any sibling job yet; 1 requirement claimed by multiple sibling jobs.",
    gaps: ["Audit log records failed logins"],
    overlapping: [{{ requirement: "Email login", jobs: [{{ id: "j1", title: "Job 1" }}, {{ id: "j2", title: "Job 2" }}] }}],
  }}
}});
console.log(JSON.stringify({{ empty1, empty2, shown }}));
"""
        out = subprocess.check_output([node, "-e", script], text=True)
        parsed = json.loads(out)
        self.assertEqual(parsed["empty1"], "")
        self.assertEqual(parsed["empty2"], "")
        self.assertIn("Sibling coverage check:", parsed["shown"])
        self.assertIn("Uncovered:", parsed["shown"])
        self.assertIn("Audit log records failed logins", parsed["shown"])
        self.assertIn("Multiple sibling jobs:", parsed["shown"])
        self.assertIn("#/jobs/j1", parsed["shown"])


if __name__ == "__main__":
    unittest.main()
