from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator/scripts"))
import reference_artifacts as refs


class ReferenceArtifactTests(unittest.TestCase):
    def test_classification_and_safe_names(self):
        for source, kind in (("https://figma.com/file/123", "figma_url"), ("mock.htm", "html_mockup"),
                             ("photo.png", "image_reference"), ("spec.pdf", "pdf_reference"),
                             ("notes.md", "text_reference"), ("https://example.com/", "url_reference"),
                             ("drawing.svg", "file_reference")):
            with self.subTest(source=source):
                self.assertEqual(refs.classify_reference(source), kind)
        self.assertEqual(refs.safe_filename("..."), "reference")
        self.assertEqual(refs.safe_filename("a b.html"), "a-b.html")
        self.assertFalse(refs.is_url("https:///path"))

    def test_url_sidecars_and_file_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.multiple(refs, ROOT=root, OUTPUT_DIR=root / "output"):
                job = {"job_id": "job-1"}
                for source in ("https://example.com/", "https://example.com/screen.html"):
                    artifact = refs.attach_reference_artifact(job, source, " note ")
                    self.assertEqual(artifact["url"], source)
                    self.assertEqual((root / artifact["path"]).read_text(), source + "\n\nnote\n")
                source = root / "screen.html"
                source.write_text("mockup")
                first = refs.attach_reference_artifact(job, "screen.html")
                second = refs.attach_reference_artifact(job, str(source))
                third = refs.attach_reference_artifact(job, "screen.html")
                self.assertEqual(first["mime_type"], "text/html")
                self.assertTrue(second["path"].endswith("screen-2.html"))
                self.assertTrue(third["path"].endswith("screen-3.html"))
                source = root / "noextension"
                source.write_text("source")
                artifact = refs.attach_reference_artifact({}, str(source))
                self.assertNotIn("mime_type", artifact)
                self.assertIn("unknown", artifact["path"])
                for name, error in (("", ValueError), ("missing.md", FileNotFoundError),
                                    ("output", FileNotFoundError), ("app.exe", ValueError)):
                    if name == "app.exe":
                        (root / name).write_text("binary")
                    with self.subTest(name=name), self.assertRaises(error):
                        refs.attach_reference_artifact(job, name)

    def test_context_includes_text_binary_urls_and_missing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.md").write_text("long text")
            (root / "photo.png").write_bytes(b"image")
            job = {"reference_artifacts": [
                {"path": "notes.md", "note": "Read this", "url": "https://example.com"},
                {"path": str(root / "photo.png"), "type": "image_reference"},
                {"path": "missing.md"}, {"url": "https://example.com"}, {},
            ]}
            with patch.multiple(refs, ROOT=root, MAX_REFERENCE_PREVIEW_CHARS=4):
                self.assertEqual(refs.reference_context({}), "")
                context = refs.reference_context(job)
            self.assertIn("long\n...[reference truncated]...", context)
            self.assertIn("Read this", context)
            self.assertIn("binary or non-text artifact", context)
            self.assertIn("https://example.com", context)
            self.assertIn("missing.md", context)
            with patch.object(refs, "ROOT", root):
                self.assertIn("long text", refs.reference_context({"reference_artifacts": [{"path": "notes.md"}]}))
