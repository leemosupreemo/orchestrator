#!/usr/bin/env python3
"""One prompt through a local Ollama model, with a context window big enough to hold it: prompt on stdin, reply on stdout.

    ollama_generate.py --model deepseek-coder [--num-ctx 16384]

`ollama run` gives every model its default window (4096 tokens on many computers) and silently drops the start of a prompt
that doesn't fit. This asks the server for a larger window instead, sized to the prompt and capped at what the model
supports (see orchestrator/ollama_api.py). When the server can't be reached it falls back to `ollama run`, which starts
what it needs or says why it can't, so nothing that worked before stops working.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import urllib.error
from typing import Any, Callable, TextIO

from orchestrator import ollama_api


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None, stderr: TextIO | None = None,
         stream: Callable[..., Any] = ollama_api.stream, runner: Callable[..., Any] = subprocess.run,
         context: Callable[[str], int | None] = ollama_api.context_length) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--num-ctx", type=int, default=0, help="ask for exactly this window (tokens) instead of sizing it to the prompt")
    args = parser.parse_args(argv)
    out, err = stdout or sys.stdout, stderr or sys.stderr
    prompt = (stdin or sys.stdin).read()
    num_ctx = args.num_ctx or ollama_api.num_ctx_for(len(prompt), context(args.model))
    started = False
    try:
        for chunk in stream(args.model, prompt, num_ctx):
            started = True
            out.write(chunk)
            out.flush()
        out.write("\n")
        return 0
    except ollama_api.OllamaError as exc:
        print(str(exc), file=err)
        return 1
    except (urllib.error.URLError, OSError):
        if started:  # it was answering and the connection dropped: a second answer would only be printed after the first
            print("The connection to Ollama dropped while it was answering.", file=err)
            return 1
        done = runner(["ollama", "run", args.model, "--nowordwrap"], input=prompt, text=True)
        return done.returncode


if __name__ == "__main__":
    sys.exit(main())
