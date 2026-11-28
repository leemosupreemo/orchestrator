"""The Documentation page groups a project's docs by kind and lists what is missing, each gap with the fix that closes it."""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator import prd, project_docs as pd


class CategoryTests(unittest.TestCase):
    def test_files_are_grouped_by_kind_with_the_core_docs_first(self):
        cases = {"README.md": "core", "ARCHITECTURE-AUDIT.md": "core", "docs/architecture.md": "core",
                 "docs/TWO-FACTION-REQUIREMENTS.md": "requirements", "docs/superpowers/specs/2026-11-01-release-policy-design.md": "requirements",
                 "docs/superpowers/plans/2026-10-24-inventory.md": "plans", "docs/HANDOFF-TWO-FACTION.md": "plans",
                 "docs/verification/Jordheim-acceptance.md": "verification", "docs/re-findings/sit-stand-timing.md": "research",
                 "docs/release-notes/v0.1.3.md": "releases", "docs/mobile-architecture-improvements.md": "other"}
        for rel, cat in cases.items():
            self.assertEqual(pd.category(rel), cat, rel)

    def test_top_level_files_named_as_documentation_are_listed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ("README.md", "ARCHITECTURE-AUDIT.md", "docs/guide.md", ".hidden.md", "secrets.md"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("# T\n")
            paths = {f["path"]: f["category"] for f in pd.project_files(root)}
            self.assertEqual(paths.get("ARCHITECTURE-AUDIT.md"), "core")
            self.assertIn("docs/guide.md", paths)
            self.assertNotIn(".hidden.md", paths)
            self.assertNotIn("secrets.md", paths)  # only top-level files whose name says they are documentation


class GapTests(unittest.TestCase):
    def gaps(self, files=None, product=None, **kw):
        files = files if files is not None else [{"path": "README.md"}, {"path": "docs/a.md"}]
        product = product or {"exists": True, "written": 6, "total": 6}
        args = {"has_architecture": True, "has_test_commands": True, **kw}
        return {g["id"]: g for g in pd.gaps(files, product, **args)}

    def test_a_complete_project_has_no_gaps(self):
        self.assertEqual(self.gaps(), {})

    def test_each_gap_comes_with_its_fix(self):
        g = self.gaps(product={"exists": True, "written": 1, "total": 6}, has_architecture=False, has_test_commands=False,
                      readme_behind=99, unmarked_plans=8)
        self.assertEqual(g["prd"]["text"], "Product requirements: 1 of 6 sections written")
        self.assertEqual(g["prd"]["fix"], {"label": "Draft from your docs", "route": "#/product?draft=1"})
        self.assertEqual(g["architecture"]["fix"]["route"], "#/product?section=architecture")
        self.assertEqual(g["readme_stale"]["text"], "README.md hasn't changed in 99 commits")
        self.assertTrue(g["readme_stale"]["fix"]["job"])
        self.assertIn("docs/build-test-commands.md", g["commands"]["text"])
        self.assertEqual(g["plans"]["text"], "8 plans don't say whether they are done")
        self.assertEqual(list(g)[:2], ["prd", "architecture"])  # most useful first

    def test_a_recently_updated_readme_and_a_missing_one_are_told_apart(self):
        self.assertNotIn("readme_stale", self.gaps(readme_behind=pd.STALE_README_COMMITS - 1))
        self.assertIn("readme", self.gaps(files=[{"path": "docs/a.md"}]))

    def test_plans_without_a_status_or_checklist_are_found(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            texts = {"docs/plans/a.md": "# A\n\nDo things.", "docs/plans/b.md": "# B\n\n**Status:** done", "docs/plans/c.md": "# C\n\n- [x] step"}
            for rel, text in texts.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            self.assertEqual(pd.plans_without_status(root, pd.project_files(root)), ["docs/plans/a.md"])

    def test_the_server_reports_a_readme_that_fell_behind_the_code(self):
        from orchestrator.web import server as ui
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            run = lambda *a: subprocess.run(["git", *a], cwd=root, check=True, capture_output=True)
            run("init", "-q"); run("config", "user.email", "t@example.test"); run("config", "user.name", "T")
            (root / "README.md").write_text("# App\n"); run("add", "."); run("commit", "-qm", "readme")
            for i in range(pd.STALE_README_COMMITS):
                (root / "code.py").write_text(str(i)); run("add", "."); run("commit", "-qm", f"c{i}")
            ids = [g["id"] for g in ui.docs_index(root)["gaps"]]
            self.assertIn("readme_stale", ids)

    def test_docs_gap_buttons_use_a_valid_job_type(self):
        from orchestrator.web import server as ui
        app = (Path(__file__).resolve().parents[1] / "orchestrator" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('runAction("new_job", { type: "infra"', app)
        self.assertIn('runAction("new_job", { type: "freestyle", summary, spec })', app)
        self.assertIn("freestyle", ui.JOB_TYPES)
        # Uniform right-aligned primary "Fix" button opens the AI / manual / import modal
        self.assertIn('<div class="side"><button type="button" class="btn small primary" data-doc-gap="${i}">Fix</button></div>', app)
        self.assertNotIn('id="proj-docs-fill"', app)
        self.assertIn("Fix documentation gap", app)
        self.assertIn('value="write">Write manually', app)
        self.assertIn('value="import">Import file', app)

    def test_write_project_file_saves_docs_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            saved = pd.write_project_file(root, "docs/build-test-commands.md", "# Build\n\n`pytest`\n")
            self.assertEqual(saved, "docs/build-test-commands.md")
            self.assertIn("pytest", (root / "docs" / "build-test-commands.md").read_text())
            self.assertEqual(pd.write_project_file(root, "README.md", "# App\n"), "README.md")
            for bad in ("../outside.md", "src/code.py", "docs/.secret.md", "docs/empty.md"):
                with self.assertRaises(pd.DocsError, msg=bad):
                    pd.write_project_file(root, bad, "" if bad.endswith("empty.md") else "# x")


class DraftReadsRequirementDocsTests(unittest.TestCase):
    def test_whole_product_requirements_come_before_per_feature_specs_and_plans_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            texts = {"docs/TWO-FACTION-REQUIREMENTS.md": "Two factions fight over keeps.", "docs/launch-design.md": "Launch flow.",
                     "docs/specs/2026-11-01-foliage-design.md": "Foliage LODs.", "docs/plans/2026-11-01-plan-design.md": "PLAN TEXT",
                     "docs/prd.md": "OLD PRD", "README.md": "# Game"}
            for rel, text in texts.items():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text)
            found = [p.relative_to(root).as_posix() for p in prd.requirement_docs(root)]
            self.assertEqual(found[0], "docs/TWO-FACTION-REQUIREMENTS.md")
            self.assertNotIn("docs/plans/2026-11-01-plan-design.md", found)
            digest = prd.project_digest(root)
            self.assertIn("Two factions fight over keeps.", digest)
            self.assertLess(digest.index("Two factions"), digest.index("Foliage LODs"))
            self.assertNotIn("OLD PRD", digest)


if __name__ == "__main__":
    unittest.main()
