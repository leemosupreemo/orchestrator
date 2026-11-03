#!/usr/bin/env python3
"""One read-only LLM turn for a job conversation: prompt on stdin, answer after a marker.

    job_chat_run.py [--model ID]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from llm import run_llm, web_capable
from model_router import get_prioritized_models

MARKER = "<<<ORCHESTRATOR-REPLY>>>"
WEB_MARKER = "<<<ORCHESTRATOR-WEB>>>"  # on/off: did the model get to search the web (printed before the reply)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=int, default=120, help="seconds the model may take")
    parser.add_argument("--web", action="store_true", help="let the model search the web, using one that can (Claude Code or the Gemini CLI)")
    parser.add_argument("--cwd", default="", help="folder the model works in (an empty scratch folder, so an agentic model can't change the project)")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    candidates = get_prioritized_models(role="reviewer")
    model = args.model or next(iter(candidates), "")
    web = False
    if args.web:
        capable = model if model and web_capable(model) else next((m for m in candidates if web_capable(m)), "")
        if capable:
            model, web = capable, True
    if not model:
        print("No model is available. Check Configuration > Models.", file=sys.stderr)
        return 1
    try:
        text, _model_used, _session = run_llm(model, prompt, cwd=Path(args.cwd) if args.cwd else None, timeout=args.timeout, role="reviewer", web=web)
    except Exception as exc:  # surfaced to the UI as the error message
        print(f"{exc}", file=sys.stderr)
        return 1
    print(WEB_MARKER + ("on" if web else "off"))
    print(MARKER)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
