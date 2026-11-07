"""Build the web UI into regular installs, such as pipx from GitHub."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildPyWithWebUI(build_py):
    def run(self) -> None:
        # Editable installs build the UI when `orchestrator ui` first needs it.
        if not getattr(self, "editable_mode", False):
            path = Path(__file__).resolve().parent / "orchestrator" / "web" / "ui_build.py"
            spec = importlib.util.spec_from_file_location("orchestrator_ui_build", path)
            ui_build = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(ui_build)
            try:
                ui_build.ensure_ui_built()
            except ui_build.UIUnavailable as error:
                print(f"Installing without the web UI: {error}")
        super().run()


setup(cmdclass={"build_py": BuildPyWithWebUI})
