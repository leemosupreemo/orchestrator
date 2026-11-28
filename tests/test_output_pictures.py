"""Pictures a job's checks save in its output folder show on the job page (a project check such as a before/after capture)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "orchestrator" / "scripts"))

from orchestrator.web import server as ui  # noqa: E402

APP = PACKAGE_ROOT / "orchestrator" / "web" / "static" / "app.js"


class OutputPicturesTests(unittest.TestCase):
    def test_the_job_lists_its_pictures_and_the_server_serves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / ".orchestrator" / "jobs"
            jobs.mkdir(parents=True)
            (jobs / "job-1.json").write_text(json.dumps({"job_id": "job-1", "title": "Greener grass", "status": "review-needed"}))
            shots = root / ".orchestrator" / "output" / "job-1" / "vegetation" / "20261127-120000" / "after"
            shots.mkdir(parents=True)
            (shots / "meadow.png").write_bytes(b"\x89PNG")
            (shots.parent / "report.md").write_text("Nothing got worse.")
            outputs = {o["path"]: o for o in ui.job_detail(root, "job-1")["outputs"]}
            picture = outputs["output/job-1/vegetation/20261127-120000/after/meadow.png"]
            self.assertEqual(picture["image"], ".orchestrator/output/job-1/vegetation/20261127-120000/after/meadow.png")
            self.assertNotIn("image", outputs["output/job-1/vegetation/20261127-120000/report.md"])
            self.assertEqual(ui.job_image(root, picture["image"]), (shots / "meadow.png").resolve())

    def test_the_page_shows_the_newest_set_with_before_beside_after(self):
        script = r"""
const fs = require('fs'), assert = require('assert');
const code = fs.readFileSync(process.argv[1], 'utf8');
const start = code.indexOf('function outputPicturesCard(');
eval(code.slice(start, code.indexOf('\n}', start) + 2));
globalThis.esc = (s) => String(s);
const o = (p) => ({path: 'output/job-1/' + p, image: '.orchestrator/output/job-1/' + p});
const html = outputPicturesCard([  // newest first, as the server lists them
  o('vegetation/2/after/meadow.png'), o('vegetation/2/before/meadow.png'), o('vegetation/2/after/grassland.png'),
  o('vegetation/2/before/grassland.png'), o('vegetation/1/after/grassland.png'), {path: 'output/job-1/test.log'}]);
const titles = [...html.matchAll(/<figcaption>([^<]+)</g)].map((m) => m[1]);
assert.deepStrictEqual(titles, ['Before · grassland', 'After · grassland', 'Before · meadow', 'After · meadow']);
assert(!html.includes('vegetation/1/'), 'an older run is not shown');
assert(html.includes('data-auth-src="job-image?path='));
assert.strictEqual(outputPicturesCard([{path: 'output/job-1/test.log'}]), '');
"""
        result = subprocess.run(["node", "-e", script, str(APP)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("${outputPicturesCard(outputs)}", APP.read_text())

    def test_project_checks_are_told_where_the_jobs_output_goes(self):
        import run_build_and_tests as rbt
        with tempfile.TemporaryDirectory() as tmp, patch.object(rbt, "OUTPUT_DIR", Path(tmp)), \
             patch.object(rbt, "run_main_build_and_tests", return_value=(False, False)), \
             patch.dict(os.environ, {}, clear=False):
            rbt.run_build_and_tests({"job_id": "job-1"}, Path(tmp) / "summary.md")
            self.assertEqual(os.environ[rbt.OUTPUT_DIR_ENV], str((Path(tmp) / "job-1").resolve()))


if __name__ == "__main__":
    unittest.main()
