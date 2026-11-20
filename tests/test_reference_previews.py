"""Generated mockups render visually without enabling artifact scripts or network access."""

from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests.test_web_ui import ServerTestCase, ui


class ReferencePreviewApiTests(ServerTestCase):
    def test_only_reference_html_and_svg_offer_visual_previews(self):
        for relative, expected in (
            ("output/design-1/references/mockup.html", True),
            ("output/design-1/references/mockup.svg", True),
            ("output/design-1/references/notes.md", False),
            ("output/design-1/other.html", False),
            ("uploads/mockup.html", False),
        ):
            with self.subTest(path=relative):
                target = self.root / ".orchestrator" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("<h1>Mockup</h1>")
                response, data = self.request("GET", f"/api/file?path={relative}")
                self.assertEqual(response.status, 200)
                self.assertEqual(bool(data.get("preview")), expected)
                self.assertEqual(data["text"], "<h1>Mockup</h1>")

    def test_truncated_files_remain_source_only(self):
        relative = "output/design-1/references/mockup.html"
        target = self.root / ".orchestrator" / relative
        target.parent.mkdir(parents=True)
        target.write_text("<h1>Mockup</h1>" * 100)
        with patch.object(ui, "MAX_FILE_BYTES", 100):
            response, data = self.request("GET", f"/api/file?path={relative}")
        self.assertEqual(response.status, 200)
        self.assertFalse(data.get("preview"))

    def test_reference_symlinks_cannot_enable_previews_of_other_runtime_files(self):
        target = self.root / ".orchestrator" / "output" / "private.html"
        target.write_text("<h1>Other output</h1>")
        link = self.root / ".orchestrator" / "output" / "design-1" / "references" / "mockup.html"
        link.parent.mkdir(parents=True)
        link.symlink_to(target)
        response, data = self.request("GET", "/api/file?path=output/design-1/references/mockup.html")
        self.assertEqual(response.status, 200)
        self.assertFalse(data.get("preview"))

    def test_html_and_svg_references_link_to_the_preview_page(self):
        context = ui.job_context(self.root, {"type": "design", "reference_artifacts": [
            {"path": ".orchestrator/output/design-1/references/mockup.html", "role": "design"},
            {"path": ".orchestrator/output/design-1/references/mockup.svg", "role": "design"},
        ]})
        self.assertEqual([item["file"] for item in context["designs"]], [
            "output/design-1/references/mockup.html", "output/design-1/references/mockup.svg",
        ])


class ReferencePreviewUiTests(unittest.TestCase):
    def test_preview_is_sandboxed_and_raw_source_remains_escaped(self):
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const pages = {};
require(require('node:path').join(require('node:path').dirname(process.argv[1]), 'html.js'));
const esc = Html.escape;
const text = '<svg onload="alert(1)"><script>alert(2)</script></svg>';
let preview = true;
const api = async () => ({text, preview});
eval(source.slice(source.indexOf('pages.file ='), source.indexOf('let projectCardOrder')));
(async () => {
  const query = new URLSearchParams({path: 'output/design-1/references/mockup.svg'});
  const visual = await pages.file(null, query);
  assert.match(visual.html, /<iframe[^>]*sandbox=""/);
  assert.match(visual.html, /referrerpolicy="no-referrer"/);
  assert.doesNotMatch(visual.html, /allow-scripts|allow-same-origin/);
  assert.ok(visual.html.includes(esc("default-src 'none'")));
  assert.ok(visual.html.includes(esc("form-action 'none'")));
  assert.match(visual.html, /srcdoc="&lt;!doctype html&gt;/);
  assert.match(visual.html, /<summary>View source<\/summary>/);
  assert.ok(visual.html.includes(esc(text)));
  assert.ok(!visual.html.includes('<script>'));
  preview = false;
  const raw = await pages.file(null, query);
  assert.doesNotMatch(raw.html, /<iframe/);
  assert.ok(raw.html.includes(esc(text)));
})().catch((error) => {console.error(error); process.exitCode = 1;});
"""
        app = Path(__file__).resolve().parents[1] / "orchestrator/web/static/app.js"
        result = subprocess.run(["node", "-e", script, str(app)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
