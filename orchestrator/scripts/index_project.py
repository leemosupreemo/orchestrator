#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT, DOCS_DIR, write_text, safe_relative_path
from orchestrator.stack_detection import detect_project_stack


def index_project():
    stack = detect_project_stack(ROOT)
    print(f"🔍 Indexing project structure in \033[97m{ROOT}\033[0m...")
    print(f"  🏗  Detected stack: {stack.display_name}")

    # 1. Identify key project files
    key_files = []
    if stack.uses_xcode and stack.xcode_project:
        key_files.append(stack.xcode_project)
    if stack.uses_xcode and stack.xcode_workspace:
        key_files.append(stack.xcode_workspace)
    if stack.manifest_file and stack.manifest_file not in ("Package.swift",):
        key_files.append(stack.manifest_file)
    if stack.manifest_file == "Package.swift":
        key_files.append("Package.swift")
    if (ROOT / "Podfile").exists():
        key_files.append("Podfile")
    if (ROOT / "Cartfile").exists():
        key_files.append("Cartfile")

    # 2. Scan top-level folders
    exclude = {".git", ".orchestrator", "docs", "build", ".build", "DerivedData",
               "node_modules", "target", "venv", ".venv", "__pycache__",
               "Pods", ".idea", ".vscode"}
    folders = [f for f in ROOT.iterdir() if f.is_dir() and f.name not in exclude and not f.name.startswith(".")]

    # 3. Build Markdown
    project_name = ROOT.name
    lines = [
        "# Project Architecture",
        "",
        "This document provides a high-level overview of the project structure for AI grounding.",
        "",
        f"**Project Type:** {stack.display_name}",
        "",
        "## Key Entry Points",
    ]
    if not key_files:
        lines.append("- (No standard entry points found)")
    else:
        for k in key_files:
            lines.append(f"- `{k}`")

    lines.extend([
        "",
        "## Folder Structure",
        "",
    ])

    if not folders:
        lines.append("- (No significant folders found in root)")
    else:
        for f in sorted(folders, key=lambda x: x.name):
            lines.append(f"### `{f.name}/`")
            # Heuristic for folder purpose
            purpose = "Contains project source code."
            name_lower = f.name.lower()
            if "test" in name_lower:
                purpose = "Contains unit, integration, or UI tests."
            elif "script" in name_lower or "tool" in name_lower:
                purpose = "Contains developer tools, build scripts, or automation."
            elif "resource" in name_lower or "asset" in name_lower:
                purpose = "Contains images, localized strings, and other assets."
            elif "core" in name_lower or "model" in name_lower:
                purpose = "Contains the core business logic and data models."
            elif "ui" in name_lower or "view" in name_lower:
                purpose = "Contains the user interface components."
            lines.append(purpose)
            lines.append("")

    lines.extend([
        "## Technology Stack",
        f"- **Language:** {stack.language.title()}",
        f"- **Framework:** {stack.framework or 'N/A'}",
    ])
    if stack.uses_xcode and stack.scheme:
        lines.append(f"- **Xcode Scheme:** {stack.scheme}")
    if stack.build_command:
        lines.append(f"- **Build Command:** `{stack.build_command}`")
    if stack.test_command:
        lines.append(f"- **Test Command:** `{stack.test_command}`")
    lines.append("")

    content = "\n".join(lines)

    arch_path = DOCS_DIR / "architecture.md"
    if arch_path.exists():
        print(f"  ⚠️  {safe_relative_path(arch_path, ROOT)} already exists. Skipping overwrite.")
    else:
        write_text(arch_path, content)
        print(f"  ✅ Drafted architecture summary to \033[97m{safe_relative_path(arch_path, ROOT)}\033[0m.")

    # 4. Also check for coding standards
    standards_path = DOCS_DIR / "coding-standards.md"
    if not standards_path.exists():
        standards_content = _coding_standards_content(stack, project_name)
        write_text(standards_path, standards_content)
        print(f"  ✅ Drafted coding standards to \033[97m{safe_relative_path(standards_path, ROOT)}\033[0m.")


def _coding_standards_content(stack, project_name: str) -> str:
    """Generate stack-appropriate coding standards content."""
    lang = stack.language
    framework = stack.framework or "N/A"
    test_cmd = stack.test_command or "N/A"

    if lang == "python":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Python Standards

- **Language Version:** Python 3.10+
- **Style Guidelines:** Follow PEP 8 and use modern type hints (`def foo(val: str) -> int:`).
- **Naming Conventions:**
  - Classes: `UpperCamelCase`
  - Functions, variables, modules: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`
- **Error Handling:** Use explicit exception types and avoid bare `except:`.
- **Testing:**
  - Write unit and integration tests using {framework}.
  - Test command: `{test_cmd}`.
"""
    if lang == "rust":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Rust Standards

- **Language Edition:** Rust 2021+
- **Formatting & Linting:** Run `cargo fmt --check` and `cargo clippy`.
- **Naming Conventions:**
  - Structs, Enums, Traits: `UpperCamelCase`
  - Functions, variables, modules: `snake_case`
  - Constants: `SCREAMING_SNAKE_CASE`
- **Error Handling:** Use `Result<T, E>` and the `?` operator; avoid `.unwrap()` in production code.
- **Testing:**
  - Unit tests in `src/` under `#[cfg(test)]`, integration tests in `tests/`.
  - Test command: `{test_cmd}`.
"""
    if lang in ("typescript", "javascript"):
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## TypeScript / JavaScript Standards

- **Language Standards:** Strict TypeScript / modern ECMAScript.
- **Formatting:** Consistent formatting (Prettier / ESLint conventions).
- **Naming Conventions:**
  - Classes, Interfaces, Types: `UpperCamelCase`
  - Functions, variables: `lowerCamelCase`
  - Constants: `UPPER_SNAKE_CASE`
- **Error Handling:** Structured error throwing and typed error handling.
- **Testing:**
  - Unit and integration tests for all features and fixes.
  - Test command: `{test_cmd}`.
"""
    if lang == "go":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Go Standards

- **Language Standards:** Go 1.20+
- **Formatting:** Always run `gofmt`.
- **Naming Conventions:**
  - Exported identifiers: `UpperCamelCase`
  - Unexported identifiers: `lowerCamelCase`
- **Error Handling:** Explicit error checking (`if err != nil`); avoid panics in libraries.
- **Testing:**
  - Unit tests in `*_test.go` with standard `testing` package.
  - Test command: `{test_cmd}`.
"""
    if lang == "csharp":
        return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Unity / C# Standards

- **Formatting:** Follow the .NET conventions already used in `Assets/`; one top-level type per file, file named after the type.
- **Naming Conventions:**
  - Types, methods, properties, public fields: `UpperCamelCase`
  - Private fields: `_lowerCamelCase`; locals and parameters: `lowerCamelCase`
  - Use `[SerializeField] private` rather than public fields to expose values in the Inspector.
- **Assets:** Commit `.meta` files alongside their assets; never edit `Library/`, `Temp/` or `Logs/`.
- **Error Handling:** Fail loudly with `Debug.LogError` or exceptions; avoid empty `catch` blocks.
- **Testing:**
  - Unity Test Framework (NUnit): `[Test]` and `[UnityTest]`; EditMode tests under `Tests/Editor`, PlayMode tests under `Tests/Runtime`.
  - Test command: `{test_cmd}`.
  - Set `UNITY_PATH` to the Unity editor binary if it is not in the Unity Hub's default location.
"""

    # Default: Swift/Xcode
    return f"""# Coding Standards

Coding standards and conventions for {project_name}.

## Swift Standards

- **Language Version:** Swift 6.0+
- **Formatting:** Follow standard Swift style guidelines (consistent indentation, clear naming).
- **Naming Conventions:**
  - Types and Protocols: `UpperCamelCase`
  - Functions, variables, and properties: `lowerCamelCase`
  - Enums: `lowerCamelCase` for cases
- **Error Handling:** Prefer explicit Swift `Error` types and structured `do/catch` over force unwrapping (`!`).
- **Testing:**
  - Write unit tests for new features and bug fixes.
  - Utilize Swift Testing (`@Test`, `#expect`) or XCTest frameworks.
  - Test command: `{test_cmd}`.
"""


if __name__ == "__main__":
    index_project()
