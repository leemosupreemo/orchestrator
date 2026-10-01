#!/usr/bin/env python3
"""One read-only LLM turn for a job conversation: prompt on stdin, answer after a marker.

    job_chat_run.py [--model ID]
"""
from __future__ import annotations

import argparse
import sys

from llm import run_llm
from model_router import get_prioritized_models

MARKER = "<<<ORCHESTRATOR-REPLY>>>"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    model = args.model or next(iter(get_prioritized_models(role="reviewer")), "")
    if not model:
        print("No model is available. Check Configuration > Models.", file=sys.stderr)
        return 1
    try:
        text, _model_used, _session = run_llm(model, prompt, timeout=120, role="reviewer")
    except Exception as exc:  # surfaced to the UI as the error message
        print(f"{exc}", file=sys.stderr)
        return 1
    print(MARKER)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
