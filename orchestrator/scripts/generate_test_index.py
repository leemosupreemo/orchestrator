#!/usr/bin/env python3
import os
import re
from pathlib import Path
from typing import Any, List, Optional

from common import ROOT
from orchestrator.project_config import PROJECT_CONFIG
from dev_console import discover_test_suites, test_command_for_suite


def _format_test_entry(s: dict, project_config) -> str:
    lang = s.get("language", "swift")
    rel_path = s["rel_path"]
    name = s["name"]

    if lang == "swift":
        target = project_config.test_target or "Tests"
        return f"- {target}/{name} (in {rel_path})"
    # Every other language: the same command the Tests page's Run button uses for this suite.
    try:
        command = test_command_for_suite(s, project_config)
        return f"- {command}" if str(rel_path) in command else f"- {command} (in {rel_path})"
    except ValueError:
        return f"- {project_config.test_command or 'run the test command'} ({rel_path})"


def generate_test_index(
    tests_dir: Path,
    likely_files: Optional[List[str]] = None,
    project_config: Optional[Any] = None,
) -> str:
    cfg = project_config or PROJECT_CONFIG
    index = []

    # Heuristic: If we have likely files, only show tests that share a keyword
    # or are recently modified.
    keywords = set()
    if likely_files:
        for f in likely_files:
            # Extract basic name without path or extension
            name = Path(f).stem
            # Split CamelCase or snake_case into parts
            parts = re.findall(r'[A-Z]?[a-z0-9]+', name)
            keywords.update(p.lower() for p in parts if len(p) > 3)

    project_root = getattr(project_config, "root", None) if project_config is not None else None
    search_root = project_root if project_root and Path(project_root).exists() else (
        tests_dir if tests_dir.exists() else ROOT
    )
    suites = discover_test_suites(search_root)

    for s in suites:
        rel_path = s["rel_path"]
        suite_name = s["name"]

        is_relevant = True
        if keywords:
            is_relevant = any(k in str(rel_path).lower() or k in suite_name.lower() for k in keywords)

        if is_relevant:
            index.append(_format_test_entry(s, cfg))

    if not index and likely_files and suites:
        # Fallback: if no matches found, show the 20 most recently modified tests
        recent = sorted(suites, key=lambda s: s["path"].stat().st_mtime if s["path"].exists() else 0, reverse=True)[:20]
        for s in recent:
            entry = _format_test_entry(s, cfg)
            index.append(f"{entry} [RECENT]")

    return "\n".join(sorted(set(index)))


if __name__ == "__main__":
    target = PROJECT_CONFIG.test_target or "Tests"
    print(f"--- {target} ---")
    test_dir = ROOT / target if (PROJECT_CONFIG.test_target and (ROOT / target).exists()) else ROOT
    print(generate_test_index(test_dir))
