"""Enforced output boundaries for mockup and test coverage jobs."""
from __future__ import annotations

import json
import platform
import re
import shlex
import shutil
from pathlib import Path, PurePosixPath

DESIGN_TYPES = {"design", "feature-design"}
TEST_TYPES = {"coverage", "test-audit"}
NONPRODUCTION_TYPES = DESIGN_TYPES | TEST_TYPES
TEST_EXTENSIONS = {".py", ".swift", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".rb", ".php", ".c", ".cpp", ".h"}


def protect_command(command: str, roots: list[Path]) -> str:
    """Make project files read-only for a process and its descendants; fail closed."""
    paths = list(dict.fromkeys(str(path.resolve()) for path in roots))
    if platform.system() == "Darwin" and (sandbox := shutil.which("sandbox-exec")):
        rules = " ".join(f"(deny file-write* (subpath {json.dumps(path)}))" for path in paths)
        profile = f"(version 1) (allow default) {rules}"
        return shlex.join([sandbox, "-p", profile, "/bin/sh", "-c", command])
    if platform.system() == "Linux" and (sandbox := shutil.which("bwrap")):
        args = [sandbox, "--die-with-parent", "--bind", "/", "/"]
        for path in paths:
            args += ["--ro-bind", path, path]
        return shlex.join([*args, "--", "/bin/sh", "-c", command])
    raise RuntimeError("Design and Tests jobs require a read-only project sandbox (sandbox-exec on macOS or bubblewrap on Linux). No model or tests were started.")


def _relative_path(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if not raw or str(path) != raw or path.is_absolute() or "\\" in raw or any(part in {".", ".."} or part.startswith(".") for part in raw.split("/")):
        raise ValueError(f"Unsafe output path: {raw}")
    return path


def is_test_file(path: PurePosixPath) -> bool:
    if path.suffix.lower() not in TEST_EXTENSIONS:
        return False
    name = path.name
    test_directory = any(part.lower() in {"test", "tests", "__tests__", "spec", "specs"} or part.endswith(("Tests", "UITests")) for part in path.parts[:-1])
    test_name = bool(re.search(r"(?:^test_.+\.py$|_test\.go$|\.(?:test|spec)\.[cm]?[jt]sx?$|(?:Test|Tests|Spec)\.(?:swift|java|kt|cs)$|_spec\.rb$)", name))
    return test_directory or test_name


def write_files(root: Path, job_type: str, job_id: str, proposals: list[dict]) -> list[str]:
    """Validate the entire batch before writing any file. Models never write the project."""
    root = root.resolve()
    if job_type not in NONPRODUCTION_TYPES or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
        raise ValueError("Invalid non-production job")
    if not isinstance(proposals, list) or not proposals:
        raise ValueError("The job must return visual mockups or test files")
    pending: dict[Path, str] = {}
    for proposal in proposals:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("path"), str) or not isinstance(proposal.get("content"), str):
            raise ValueError("Each output needs a path and text content")
        relative = _relative_path(proposal["path"])
        if job_type in DESIGN_TYPES:
            if len(relative.parts) != 1 or relative.suffix.lower() not in {".html", ".svg"}:
                raise ValueError("Design jobs can only produce self-contained HTML or SVG visual mockups")
            relative = PurePosixPath("docs/product/designs", job_id, relative.name)
        elif not is_test_file(relative):
            raise ValueError(f"Tests jobs cannot change production files: {relative}")
        target = root / relative
        if any(part.is_symlink() for part in [target, *target.parents] if part != root.parent):
            raise ValueError(f"Output paths cannot contain symlinks: {relative}")
        if not target.resolve().is_relative_to(root) or target in pending:
            raise ValueError(f"Unsafe or duplicate output: {relative}")
        if target.exists() and not target.is_file():
            raise ValueError(f"Output is not a file: {relative}")
        if target.exists() and target.stat().st_nlink > 1:
            raise ValueError(f"Output cannot be a hardlink: {relative}")
        pending[target] = proposal["content"]
    for target, content in pending.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return [str(target.relative_to(root)) for target in pending]
