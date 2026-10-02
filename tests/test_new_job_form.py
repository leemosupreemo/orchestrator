from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import new_job_form as N  # noqa: E402


class ComposeTests(unittest.TestCase):
    def test_bug_matches_the_terminal_flow(self):
        short, spec = N.compose("bug", {"summary": "Seat empty after rejoin", "repro": "1. Join\n2. Background", "expected": "Seat is kept"})
        self.assertEqual(short, "Seat empty after rejoin")
        self.assertEqual(spec, "SUMMARY: Seat empty after rejoin\n\nREPRO STEPS:\n1. Join\n2. Background\n\nEXPECTED BEHAVIOR:\nSeat is kept")

    def test_optional_bug_fields_may_be_blank(self):
        _, spec = N.compose("bug", {"summary": "Crash"})
        self.assertIn("REPRO STEPS:\n\n", spec)
        self.assertTrue(spec.endswith("EXPECTED BEHAVIOR:\n"))

    def test_design_carries_the_look_and_feel_with_a_default(self):
        self.assertIn("PREFERRED VIBE: glassmorphism", N.compose("design", {"summary": "Profile screen", "vibe": "glassmorphism"})[1])
        self.assertIn(f"PREFERRED VIBE: {N.DEFAULT_VIBE}", N.compose("design", {"summary": "Profile screen"})[1])
        self.assertTrue(N.compose("design", {"summary": "Profile screen", "vibe": "Cyberpunk, neon"})[1].startswith("DESIGN VISION: Profile screen"))

    def test_coverage_and_feature(self):
        self.assertEqual(N.compose("coverage", {"summary": "Lobby", "subsystems": "Seat service"})[1], "COVERAGE FOCUS: Lobby\n\nSUBSYSTEMS: Seat service")
        self.assertIn("SUBSYSTEMS: Lobby", N.compose("coverage", {"summary": "Lobby"})[1])
        _, spec = N.compose("feature", {"summary": "Add rematch", "details": "Either player can ask"})
        self.assertEqual(spec, "VISION: Add rematch\n\nADDITIONAL DETAILS:\nEither player can ask")

    def test_quick_is_just_the_instruction(self):
        self.assertEqual(N.compose("quick", {"summary": "Rename Foo to Bar everywhere\nand update docs"}), ("Rename Foo to Bar everywhere\nand update docs", ""))

    def test_the_title_is_the_first_line_and_capped(self):
        short, _ = N.compose("feature", {"summary": "x" * 600 + "\nsecond"})
        self.assertEqual(len(short), 500)

    def test_a_summary_is_required(self):
        for kind in ("bug", "feature", "design", "coverage", "quick"):
            with self.assertRaises(N.FormError):
                N.compose(kind, {"summary": "  "})


class UploadTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual([N.upload_kind(n) for n in ("a.PNG", "b.jpeg", "c.log", "d.txt", "e.fig", "f.pdf", "g.html", "h.ips")],
                         ["image", "image", "log", "log", "design", "design", "design", "log"])

    def test_unsupported_types_are_rejected(self):
        for name in ("run.exe", "script.sh", "archive.zip", "noextension", "x.svg"):
            with self.assertRaises(N.FormError):
                N.upload_kind(name)

    def test_reference_types(self):
        self.assertEqual([N.reference_type(p) for p in ("a.png", "b.html", "c.pdf", "d.md", "e.fig")],
                         ["image_reference", "html_mockup", "pdf_reference", "text_reference", "file_reference"])


class AttachmentsTests(unittest.TestCase):
    def test_empty_is_empty(self):
        self.assertEqual(N.attachments_block([], []), "")

    def test_logs_are_excerpted_from_the_end_and_files_are_listed(self):
        text = "old\n" * 5000 + "THE ERROR"
        block = N.attachments_block([("run.log", text)], [("mock.png", "image_reference", ".orchestrator/ui/uploads/x-mock.png")])
        self.assertIn("THE ERROR", block)
        self.assertIn("(showing the end of the log)", block)
        self.assertLess(len(block), N.LOG_EXCERPT_CHARS + 1000)
        self.assertIn("- mock.png (image_reference): `.orchestrator/ui/uploads/x-mock.png`", block)

    def test_many_logs_are_summarised(self):
        block = N.attachments_block([(f"{i}.log", "x") for i in range(5)], [])
        self.assertEqual(block.count("### Log:"), N.MAX_LOG_BLOCKS)
        self.assertIn("+2 more logs", block)


if __name__ == "__main__":
    unittest.main()
