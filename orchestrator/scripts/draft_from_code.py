#!/usr/bin/env python3
"""Draft a product requirements doc (PRD) from an existing codebase.

Reads a bounded summary of the project (README, docs, manifests, file layout,
source file types, recent commits), asks a model to draft a short PRD from it,
and shows the proposal as a diff. Accepts "yes" to save it, or discards.

    draft_from_code.py [--root PATH] [--model MODEL] [--dry-run] [--yes]
"""
from __future__ import annotations

import argparse
import difflib
import subprocess
import sys
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT
from orchestrator.prd import (
    can_draft,
    draft_prompt,
    parse_draft,
    project_digest,
    template,
)
from orchestrator.prd import PATH as PRD_RELPATH
from llm import run_llm
from model_router import get_prioritized_models

FORMAT_REMINDER = (
    "\n\n### FORMAT CORRECTION ###\nYour previous reply could not be read. Reply again with ONLY the JSON object, "
    "no commentary before or after it, no code fence, and every quote inside the markdown escaped:\n"
    '{{"summary": "bulleted: what was inferred, and any gaps or low-confidence guesses that were hard to figure out", '
    '"markdown": "the complete document"}}'
)


def _recent_commits(root: Path) -> list[str]:
    try:
        res = subprocess.run(
            ["git", "log", "--format=%s", "-n", "30"],
            cwd=str(root), capture_output=True, text=True, timeout=10,
        )
        return [line for line in res.stdout.strip().splitlines() if line] if res.returncode == 0 else []
    except Exception:
        return []


def _default_model() -> str:
    candidates = get_prioritized_models(role="reviewer")
    return next(iter(candidates), "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=str(ROOT), help="Project root path")
    parser.add_argument("--model", default="", help="Model ID or alias (defaults to the best available reviewer model)")
    parser.add_argument("--dry-run", action="store_true", help="Draft the PRD to stdout without writing any files")
    parser.add_argument("--yes", action="store_true", help="Accept and save the proposal without prompting")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        return 1

    # Check there is something to analyze
    if not can_draft(root):
        print("Nothing readable found to draft a PRD from (no README, docs, manifest, or source files).")
        print("Describe the project in your own words instead: create docs/product/prd.md by hand.")
        return 1

    # Compute the digest and current PRD content
    print(f"\n📖 Analyzing codebase at {root}...")
    commits = _recent_commits(root)
    digest = project_digest(root, commits)
    print(f"  Read {len(digest)} characters of project context.")

    prd_path = root / PRD_RELPATH
    current = prd_path.read_text(encoding="utf-8") if prd_path.exists() else template()

    # Build the prompt and call the model
    prompt = draft_prompt(digest, current)
    model = args.model or _default_model()
    if not model:
        print("No AI model is available. Configure at least one model (e.g. --models codex in the wizard, or set an API key).")
        return 1

    print(f"\n🧠 Drafting product requirements with model: {model}")
    print("    (this may take 30–60 seconds…)\n")
    sys.stdout.flush()

    try:
        raw_reply, model_used, _ = run_llm(model, prompt, cwd=root, timeout=270, role="reviewer")
    except Exception as exc:
        print(f"Error calling model: {exc}", file=sys.stderr)
        return 1

    # Parse the model's reply; retry once if the format was wrong
    try:
        proposal = parse_draft(raw_reply, current)
    except Exception:
        print("    Model reply wasn't in the expected format. Asking again…")
        try:
            raw_reply, model_used, _ = run_llm(model, prompt + FORMAT_REMINDER, cwd=root, timeout=270, role="reviewer")
            proposal = parse_draft(raw_reply, current)
        except Exception as exc:
            print(f"Error: the model couldn't produce a readable PRD draft: {exc}", file=sys.stderr)
            return 1

    print(f"    Model: {model_used}")
    print(f"    Summary: {proposal.get('summary', '(no summary)')}")

    if args.dry_run:
        print("\n--- Proposed PRD ---\n")
        print(proposal["markdown"])
        print("\n--- Diff against current ---\n")
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            proposal["markdown"].splitlines(keepends=True),
            fromfile="current",
            tofile="proposed",
            n=2,
        )
        sys.stdout.write("".join(diff))
        return 0

    # Show a diff and ask to accept
    print("\n📋 Proposed PRD (diff):\n")
    diff_lines = difflib.unified_diff(
        current.splitlines(keepends=True),
        proposal["markdown"].splitlines(keepends=True),
        fromfile=str(prd_path.relative_to(root)) if prd_path.exists() else "(new file)",
        tofile="proposed",
        n=2,
    )
    diff_text = "".join(diff_lines)
    if diff_text:
        for line in diff_lines:
            if line.startswith("+") and not line.startswith("+++"):
                print(f"\033[92m{line}\033[0m", end="")
            elif line.startswith("-") and not line.startswith("---"):
                print(f"\033[91m{line}\033[0m", end="")
            else:
                print(line, end="")
    else:
        print("  (no changes)")

    if args.yes:
        accept = True
    else:
        try:
            choice = input("\nSave this PRD? [y/N] ").strip().lower()
            accept = choice in ("y", "yes")
        except EOFError:
            accept = True

    if not accept:
        print("Discarded. No file was written.")
        return 0

    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(proposal["markdown"], encoding="utf-8")
    print(f"\n✅ Saved PRD to {prd_path.relative_to(root)}")
    print("\nNext: break it down into epics with `orchestrator console`, or review it and create your first job.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
