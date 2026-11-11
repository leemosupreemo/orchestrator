#!/usr/bin/env python3
"""One read-only LLM turn for a job conversation: prompt on stdin, answer after a marker.

    job_chat_run.py [--model ID] [--allow ID,ID,...]

With --allow, only those models are tried, in that order: the person chose a kind of model (Ask AI's Free, Balanced or
Complex) and an answer from another kind, say a paid one for a free question, would break that promise.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from llm import run_llm, web_capable
from model_router import get_prioritized_models

MARKER = "<<<ORCHESTRATOR-REPLY>>>"
WEB_MARKER = "<<<ORCHESTRATOR-WEB>>>"
STAGE_MARKER = "<<<ORCHESTRATOR-STAGE>>>"  # a progress line for the page, before the reply
MODEL_MARKER = "<<<ORCHESTRATOR-MODEL>>>"  # the model that answered (printed before the reply)
MAX_FALLBACKS = 2  # on/off: did the model get to search the web (printed before the reply)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout", type=int, default=120, help="seconds the model may take")
    parser.add_argument("--web", action="store_true", help="let the model search the web, using one that can (Claude Code or the Gemini CLI)")
    parser.add_argument("--allow", default="", help="comma-separated models: try only these, in this order")
    parser.add_argument("--cwd", default="", help="folder the model works in (an empty scratch folder, so an agentic model can't change the project)")
    args = parser.parse_args(argv)
    prompt = sys.stdin.read()
    allow = [m.strip() for m in args.allow.split(",") if m.strip()]
    candidates = allow or get_prioritized_models(role="reviewer")
    model = args.model or next(iter(candidates), "")
    web = False
    if args.web:
        capable = model if model and web_capable(model) else next((m for m in candidates if web_capable(m)), "")
        if capable:
            model, web = capable, True
    if not model:
        print("No model is available. Check Configuration > Models.", file=sys.stderr)
        return 1
    # A couple of fallbacks of the same kind, not the whole catalog: each one can take the full timeout.
    fallbacks = [m for m in candidates if m != model and (web_capable(m) or not web)][:MAX_FALLBACKS]
    if allow and model not in allow:
        print("That model isn't one of the ones this question may use.", file=sys.stderr)
        return 1
    print(f"{STAGE_MARKER}{model}{' with web search' if web else ''}", flush=True)
    try:
        text, model_used, _session = run_llm(model, prompt, cwd=Path(args.cwd) if args.cwd else None, timeout=args.timeout, role="reviewer", web=web,
                                              allowed_models=[model, *fallbacks], raw=True)
    except Exception as exc:  # surfaced to the UI as the error message
        print(f"{exc}", file=sys.stderr)
        return 1
    print(WEB_MARKER + ("on" if web else "off"))
    print(MODEL_MARKER + (model_used or model))
    print(MARKER)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
