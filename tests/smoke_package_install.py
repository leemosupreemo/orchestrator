#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


import os

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> None:
    print("$ " + " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd or PACKAGE_ROOT), env=env, check=True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="orchestrator-install-") as temp_dir_str:
        temp_dir = Path(temp_dir_str)
        venv = temp_dir / "venv"
        project = temp_dir / "FixtureApp"
        project.mkdir()
        (project / "FixtureApp.xcodeproj").mkdir()
        user_state = temp_dir / "user_state"
        user_state.mkdir()

        env = os.environ.copy()
        env["ORCHESTRATOR_USER_STATE_DIR"] = str(user_state)

        run([sys.executable, "-m", "venv", str(venv)], env=env)
        python = venv / "bin" / "python"
        orchestrator = venv / "bin" / "orchestrator"

        run([str(python), "-m", "pip", "install", "-e", str(PACKAGE_ROOT)], env=env)
        run([str(orchestrator), "--help"], env=env)
        run([str(orchestrator), "init", "--root", str(project), "--project-name", "FixtureApp", "--force"], env=env)
        run([str(orchestrator), "check-config"], cwd=project, env=env)

        config_path = project / ".orchestrator" / "project.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["project_name"] == "FixtureApp", config
        assert config["xcode_project"] == "FixtureApp.xcodeproj", config
        gitignore = (project / ".orchestrator" / ".gitignore").read_text(encoding="utf-8")
        assert "jobs/" in gitignore, gitignore
        assert "logs/" in gitignore, gitignore

    print("Package install smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
