from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "orchestrator" / "scripts"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import smoke_test_delivery  # noqa: E402


class DeliveryWorkflowTests(unittest.TestCase):
    def test_incomplete_preflight_does_not_mutate_distribution_setup(self) -> None:
        config = MagicMock()
        config.validate_distribution_config.return_value = ["missing signing"]
        config.runtime_dir = Path(".orchestrator")

        with (
            patch.object(smoke_test_delivery, "PROJECT_CONFIG", config),
            patch("smoke_test_delivery.ensure_keychain_unlocked", return_value=(True, "unlocked")),
            patch("smoke_test_delivery.prompt_confirm", return_value=False),
            patch("setup_distribution.setup_distribution") as mock_setup,
            self.assertRaises(SystemExit) as exit_context,
        ):
            smoke_test_delivery.run_smoke_delivery()

        self.assertEqual(exit_context.exception.code, 1)
        mock_setup.assert_not_called()

    def test_web_delivery_with_missing_signing_exits_without_launching_wizard(self) -> None:
        config = MagicMock()
        config.validate_distribution_config.return_value = [
            "Apple Development Team ID (development_team) is not set.",
            "Distribution method (delivery_method) is not set.",
        ]
        config.runtime_dir = Path(".orchestrator")
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"ORCHESTRATOR_NONINTERACTIVE": "1"}),
            patch.object(smoke_test_delivery, "PROJECT_CONFIG", config),
            patch.object(smoke_test_delivery.subprocess, "run") as wizard,
            contextlib.redirect_stdout(output),
            self.assertRaises(SystemExit) as exit_context,
        ):
            smoke_test_delivery.run_smoke_delivery()

        self.assertEqual(exit_context.exception.code, 1)
        wizard.assert_not_called()
        self.assertIn("development_team", output.getvalue())
        self.assertIn("delivery_method", output.getvalue())
        self.assertIn("orchestrator wizard", output.getvalue())
        self.assertNotIn("Please choose one of the options", output.getvalue())

    def test_quick_delivery_uses_direct_delivery_metadata(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-delivery-") as temp_dir:
            root = Path(temp_dir)
            jobs = root / ".orchestrator" / "jobs"
            jobs.mkdir(parents=True)
            config = MagicMock()
            config.validate_distribution_config.return_value = []
            captured_job: dict[str, object] = {}

            def capture_job(command, **_kwargs):
                captured_job.update(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
                return 0

            with (
                patch.object(smoke_test_delivery, "ROOT", root),
                patch.object(smoke_test_delivery, "JOBS_DIR", jobs),
                patch.object(smoke_test_delivery, "PROJECT_CONFIG", config),
                patch("smoke_test_delivery.ensure_keychain_unlocked", return_value=(True, "unlocked")),
                patch("smoke_test_delivery.subprocess.check_output", return_value=b"feature/current\n"),
                patch("smoke_test_delivery.subprocess.call", side_effect=capture_job),
            ):
                smoke_test_delivery.run_smoke_delivery()

            self.assertEqual(captured_job["delivery_kind"], "quick")
            self.assertNotIn("issue_number", captured_job)

    def test_unexpected_delivery_error_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory(prefix="orchestrator-delivery-") as temp_dir:
            root = Path(temp_dir)
            jobs = root / ".orchestrator" / "jobs"
            jobs.mkdir(parents=True)
            config = MagicMock()
            config.validate_distribution_config.return_value = []

            with (
                patch.object(smoke_test_delivery, "ROOT", root),
                patch.object(smoke_test_delivery, "JOBS_DIR", jobs),
                patch.object(smoke_test_delivery, "PROJECT_CONFIG", config),
                patch("smoke_test_delivery.ensure_keychain_unlocked", return_value=(True, "unlocked")),
                patch("smoke_test_delivery.subprocess.check_output", return_value=b"feature/current\n"),
                patch("smoke_test_delivery.subprocess.call", side_effect=RuntimeError("delivery crashed")),
                self.assertRaises(SystemExit) as exit_context,
            ):
                smoke_test_delivery.run_smoke_delivery()

            self.assertEqual(exit_context.exception.code, 1)
            self.assertEqual(list(jobs.glob("*.json")), [])


if __name__ == "__main__":
    unittest.main()
