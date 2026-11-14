"""Talking to a local Ollama server, for the one thing `ollama run` can't do: choose the context window.

Ollama gives every model a fixed window (4096 tokens by default on a computer with a modest GPU) and silently drops what
doesn't fit. A long prompt loses its beginning, and an Ask AI prompt begins with the project, so the model answered as if it
had never heard of it. `ollama run` has no setting for this; the HTTP API takes `num_ctx` per request.

So a prompt that fits the default window still goes through `ollama run`, exactly as before, and a longer one goes through
`orchestrator script ollama_generate.py`, which asks for a window big enough (up to what the model supports).
"""
from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Callable, Iterator

CHARS_PER_TOKEN = 3          # a cautious guess for prose and code mixed; real text is usually 3.5 to 4
ANSWER_ROOM = 1500           # tokens kept free for the reply
DEFAULT_WINDOW = 4096        # what Ollama gives a model unless asked for more
MAX_WINDOW = 32768           # never ask for more than this: memory grows with it
RUN_MAX_CHARS = (DEFAULT_WINDOW - ANSWER_ROOM) * CHARS_PER_TOKEN - 500   # the longest prompt `ollama run` can be trusted with
_CACHE: dict[str, tuple[float, int | None]] = {}
CACHE_SECONDS = 600

Opener = Callable[..., Any]


class OllamaError(Exception):
    """Something Ollama said, or a reason it couldn't be asked, in words a person can read."""


def base_url(env: dict[str, str] | None = None) -> str:
    """The server's address from OLLAMA_HOST (which may omit the scheme or the port), else the local default."""
    host = ((env if env is not None else os.environ).get("OLLAMA_HOST") or "").strip() or "127.0.0.1:11434"
    if "://" not in host:
        host = "http://" + host
    parts = urlsplit(host)
    name = parts.hostname or "127.0.0.1"
    name = f"[{name}]" if ":" in name else name
    return f"{parts.scheme or 'http'}://{name}:{parts.port or 11434}"


def model_for(model_id: str, required_clis: Any = (), family: str = "") -> str | None:
    """The Ollama model name this registry model runs as, or None when it doesn't run through `ollama run`."""
    if model_id == "deepseek":
        return "deepseek-coder"
    if "ollama" in (required_clis or ()) or family in ("ollama", "qwen"):
        return model_id
    return None


def _post(path: str, payload: dict[str, Any], timeout: float, opener: Opener = urllib.request.urlopen, env: dict[str, str] | None = None):
    request = urllib.request.Request(base_url(env) + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    return opener(request, timeout=timeout)


def context_length(model: str, *, timeout: float = 3.0, opener: Opener = urllib.request.urlopen, now: float | None = None,
                   env: dict[str, str] | None = None) -> int | None:
    """The most tokens this model supports, from the server, or None when it can't be asked. Remembered for ten minutes."""
    clock = now if now is not None else time.time()
    cached = _CACHE.get(model)
    if cached and clock - cached[0] < CACHE_SECONDS:
        return cached[1]
    found: int | None = None
    try:
        with _post("/api/show", {"model": model}, timeout, opener, env) as response:
            info = json.loads(response.read().decode("utf-8", "replace")).get("model_info") or {}
        values = [v for k, v in info.items() if str(k).endswith(".context_length") and isinstance(v, int) and v > 0]
        found = max(values) if values else None
    except (OSError, ValueError, urllib.error.URLError):
        found = None
    _CACHE[model] = (clock, found)
    return found


WINDOW_STEPS = (4096, 8192, 16384, 32768)  # a few fixed sizes: Ollama reloads a model whenever the size it is asked for changes


def num_ctx_for(prompt_chars: int, model_max: int | None) -> int:
    """The window to ask for: the smallest step that holds the prompt and room for the answer, never past what the model (and memory) allows."""
    wanted = math.ceil(prompt_chars / CHARS_PER_TOKEN) + ANSWER_ROOM
    ceiling = min(model_max or MAX_WINDOW, MAX_WINDOW)
    fitting = next((step for step in WINDOW_STEPS if step >= wanted), WINDOW_STEPS[-1])
    return max(min(DEFAULT_WINDOW, ceiling), min(fitting, ceiling))


def window_chars(model: str, **kw: Any) -> int | None:
    """How many characters of prompt this local model can take in whole (with the biggest window we'd ask for), or None when unknown."""
    limit = context_length(model, **kw)
    if not limit:
        return None
    return max(2_000, (min(limit, MAX_WINDOW) - ANSWER_ROOM) * CHARS_PER_TOKEN)


def stream(model: str, prompt: str, num_ctx: int, *, timeout: float = 600.0, opener: Opener = urllib.request.urlopen,
           env: dict[str, str] | None = None) -> Iterator[str]:
    """The reply as it arrives. Raises OllamaError with Ollama's own message when it refuses, URLError when it can't be reached."""
    payload = {"model": model, "prompt": prompt, "stream": True, "options": {"num_ctx": num_ctx}}
    try:
        response = _post("/api/generate", payload, timeout, opener, env)
    except urllib.error.HTTPError as exc:
        try:
            said = json.loads(exc.read().decode("utf-8", "replace")).get("error")
        except (ValueError, AttributeError):
            said = None
        raise OllamaError(str(said or f"Ollama answered with an error ({exc.code})").strip()) from exc
    with response:
        for raw in response:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get("error"):
                raise OllamaError(str(item["error"]).strip())
            if item.get("response"):
                yield item["response"]
            if item.get("done"):
                return
