from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DetectedStack:
    language: str              # "swift", "python", "rust", "typescript", "javascript", "go", "generic"
    framework: str | None      # "xcode", "spm", "pytest", "unittest", "cargo", "npm", "yarn", "pnpm", "bun", "go test"
    display_name: str          # e.g. "Swift (Xcode)", "Rust (Cargo)", "Python (pytest)", "TypeScript (npm)", "Go"
    build_command: str | None  # e.g. "cargo build", "npm run build", "go build ./..."
    test_command: str | None   # e.g. "cargo test", "npm test", "go test ./..."
    uses_xcode: bool           # True only if Xcode project/workspace or Apple tooling
    scheme: str | None = None
    schemes: list[str] = field(default_factory=list)
    test_target: str | None = None
    targets: list[str] = field(default_factory=list)
    xcode_project: str | None = None
    xcode_workspace: str | None = None
    manifest_file: str | None = None


def detect_project_stack(root: Path) -> DetectedStack:
    """
    Inspects a repository root to determine its primary language, toolchain,
    default build/test commands, and whether it relies on Xcode.
    """
    # 1. Xcode Project or Workspace
    xcode_proj = next(iter(sorted(root.glob("*.xcodeproj"))), None)
    xcode_ws = next(iter(sorted(root.glob("*.xcworkspace"))), None)
    if xcode_proj or xcode_ws:
        schemes, targets = _inspect_xcode_container(root, xcode_proj, xcode_ws)
        fallback_scheme = (xcode_proj or xcode_ws).stem if (xcode_proj or xcode_ws) else root.name
        scheme = schemes[0] if schemes else fallback_scheme
        test_target = next((t for t in targets if t.endswith("Tests")), f"{scheme}Tests")
        return DetectedStack(
            language="swift",
            framework="xcode",
            display_name="Swift (Xcode)",
            build_command=None,  # Handled by xcodebuild logic
            test_command=None,
            uses_xcode=True,
            scheme=scheme,
            schemes=schemes,
            test_target=test_target,
            targets=targets,
            xcode_project=xcode_proj.name if xcode_proj else None,
            xcode_workspace=xcode_ws.name if xcode_ws else None,
            manifest_file=(xcode_ws or xcode_proj).name if (xcode_ws or xcode_proj) else None,
        )

    # 2. Swift Package Manager (standalone, non-Xcode)
    if (root / "Package.swift").exists():
        return DetectedStack(
            language="swift",
            framework="spm",
            display_name="Swift (SPM)",
            build_command="swift build",
            test_command="swift test",
            uses_xcode=False,
            manifest_file="Package.swift",
        )

    # 3. Rust (Cargo)
    if (root / "Cargo.toml").exists():
        return DetectedStack(
            language="rust",
            framework="cargo",
            display_name="Rust (Cargo)",
            build_command="cargo build",
            test_command="cargo test",
            uses_xcode=False,
            manifest_file="Cargo.toml",
        )

    # 4. Python
    pyproject = root / "pyproject.toml"
    setup_py = root / "setup.py"
    setup_cfg = root / "setup.cfg"
    reqs_txt = root / "requirements.txt"
    pipfile = root / "Pipfile"
    has_python = any(f.exists() for f in (pyproject, setup_py, setup_cfg, reqs_txt, pipfile))
    if not has_python:
        # Check if root has .py files or src/*.py
        has_python = bool(list(root.glob("*.py"))) or bool(list((root / "src").glob("*.py"))) if (root / "src").is_dir() else False

    if has_python:
        # Determine test runner (pytest vs unittest)
        uses_pytest = False
        for path in (pyproject, setup_cfg, reqs_txt):
            if path.exists():
                try:
                    if "pytest" in path.read_text(encoding="utf-8", errors="ignore").lower():
                        uses_pytest = True
                        break
                except Exception:
                    pass

        build_cmd = "python3 -m compileall -q src" if (root / "src").is_dir() else "python3 -m compileall -q ."
        test_cmd = "pytest" if uses_pytest else "python3 -m unittest discover tests"
        framework = "pytest" if uses_pytest else "unittest"
        manifest = "pyproject.toml" if pyproject.exists() else ("setup.py" if setup_py.exists() else ("requirements.txt" if reqs_txt.exists() else "Python"))

        return DetectedStack(
            language="python",
            framework=framework,
            display_name=f"Python ({framework})",
            build_command=build_cmd,
            test_command=test_cmd,
            uses_xcode=False,
            manifest_file=manifest,
        )

    # 5. Node / TypeScript
    package_json = root / "package.json"
    if package_json.exists():
        pkg_data: dict[str, Any] = {}
        try:
            pkg_data = json.loads(package_json.read_text(encoding="utf-8"))
        except Exception:
            pass

        scripts = pkg_data.get("scripts", {}) if isinstance(pkg_data, dict) else {}
        is_ts = (root / "tsconfig.json").exists() or any(root.glob("src/**/*.ts"))
        lang = "typescript" if is_ts else "javascript"

        # Determine package manager
        pm = "npm"
        if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            pm = "bun"
        elif (root / "pnpm-lock.yaml").exists():
            pm = "pnpm"
        elif (root / "yarn.lock").exists():
            pm = "yarn"

        build_cmd = f"{pm} run build" if "build" in scripts else None
        test_cmd = f"{pm} test" if "test" in scripts else f"{pm} test"
        display = f"TypeScript ({pm})" if is_ts else f"Node.js ({pm})"

        return DetectedStack(
            language=lang,
            framework=pm,
            display_name=display,
            build_command=build_cmd,
            test_command=test_cmd,
            uses_xcode=False,
            manifest_file="package.json",
        )

    # 6. Go
    if (root / "go.mod").exists():
        return DetectedStack(
            language="go",
            framework="go test",
            display_name="Go",
            build_command="go build ./...",
            test_command="go test ./...",
            uses_xcode=False,
            manifest_file="go.mod",
        )

    # 7. Generic / Unknown Stack
    return DetectedStack(
        language="generic",
        framework=None,
        display_name="Generic / Other",
        build_command=None,
        test_command=None,
        uses_xcode=False,
        manifest_file=None,
    )


def _inspect_xcode_container(
    root: Path, xcode_proj: Path | None, xcode_ws: Path | None
) -> tuple[list[str], list[str]]:
    """Runs xcodebuild -list -json if available to extract schemes and targets."""
    if not shutil.which("xcodebuild"):
        return [], []

    import subprocess
    args = ["xcodebuild", "-list", "-json"]
    if xcode_ws:
        args.extend(["-workspace", xcode_ws.name])
    elif xcode_proj:
        args.extend(["-project", xcode_proj.name])
    else:
        return [], []

    try:
        res = subprocess.run(
            args,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            data = json.loads(res.stdout)
            container = data.get("workspace") or data.get("project") or {}
            schemes = sorted(container.get("schemes") or [])
            targets = sorted(container.get("targets") or [])
            return schemes, targets
    except Exception:
        pass
    return [], []
