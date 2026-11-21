#!/usr/bin/env python3
from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from common import CONFIG_DIR, LOGS_DIR, ROOT, append_log, ProgressIndicator, colorize_diff_line, LoopTroubleDetector
from model_registry import get_model, ModelTier, get_all_models, preferred_cli, summarize_cli_error, model_usable, model_substitute, runnable_here
from model_router import get_prioritized_models, ModelRole, ROLE_REQUIREMENTS
from orchestrator import usage_limits
from orchestrator.model_attempts import AttemptAudit

class LLMTimeoutError(RuntimeError):
    """Raised when an LLM call exceeds its timeout."""
    def __init__(self, model: str, timeout: int, duration: float, input_chars: int, output_chars: int, last_milestone: str):
        self.model = model
        self.timeout = timeout
        self.duration = duration
        self.input_chars = input_chars
        self.output_chars = output_chars
        self.last_milestone = last_milestone
        super().__init__(f"{model} timed out after {timeout}s")

# For backward compatibility with scripts that still expect these
all_m = get_all_models()
SUPPORTED_MODELS = {m.id for m in all_m}
for m in all_m:
    SUPPORTED_MODELS.update(m.aliases)

DEFAULT_FALLBACKS = get_prioritized_models()

def extract_json_block(text: str) -> str:
    # Strip ANSI escape sequences (CSI, OSC, cursor controls) so they do not break JSON decoding
    text = re.sub(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\^\[\[?[A-Za-z0-9_~]*', '', text).strip()

    def _extract_balanced_span(s: str, opener: str, closer: str) -> tuple[str | None, int, int]:
        start = s.find(opener)
        if start == -1:
            return None, -1, -1
        stack = 0
        in_string = False
        escape = False
        for i in range(start, len(s)):
            c = s[i]
            if c == '"' and not escape:
                in_string = not in_string
            elif not in_string:
                if c == opener:
                    stack += 1
                elif c == closer:
                    stack -= 1
                    if stack == 0:
                        return s[start:i+1].strip(), start, i + 1
            if c == '\\' and not escape:
                escape = True
            else:
                escape = False
        return None, -1, -1

    def _strip_comments(value: str) -> str:
        # A URL or source-code comment inside a JSON string is literal content.
        return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*',
                      lambda match: "" if match.group().startswith("//") else match.group(), value).strip()

    # 1. Try markdown fences (all blocks)
    fenced_blocks = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced_blocks:
        # Prioritize any fenced block that parses as a JSON dict
        for b in fenced_blocks:
            b_clean = _strip_comments(b.strip())
            try:
                parsed = json.loads(b_clean)
                if isinstance(parsed, dict):
                    return b_clean
            except Exception:
                pass
        # Next, try any fenced block that parses as valid JSON (e.g. list)
        for b in fenced_blocks:
            b_clean = _strip_comments(b.strip())
            try:
                json.loads(b_clean)
                return b_clean
            except Exception:
                pass
        text = fenced_blocks[0].strip()
    else:
        # 2. Stack-based extraction:
        dict_cand, dict_start, dict_end = _extract_balanced_span(text, '{', '}')
        list_cand, list_start, list_end = _extract_balanced_span(text, '[', ']')

        dict_valid = False
        dict_clean = ""
        if dict_cand:
            dict_clean = _strip_comments(dict_cand)
            try:
                if isinstance(json.loads(dict_clean), dict):
                    dict_valid = True
            except Exception:
                pass

        list_valid = False
        list_clean = ""
        if list_cand:
            list_clean = _strip_comments(list_cand)
            try:
                json.loads(list_clean)
                list_valid = True
            except Exception:
                pass

        # If list_cand encloses dict_cand and is valid JSON, the top-level structure is the list!
        if list_valid and dict_valid and list_start <= dict_start and list_end >= dict_end:
            return list_clean

        # If dict_valid is true (e.g. dict after list, or no valid list), prioritize the dict
        if dict_valid:
            return dict_clean

        # If only list_valid is true
        if list_valid:
            return list_clean

        if dict_cand:
            text = dict_cand
        elif list_cand:
            text = list_cand

    # 3. Strip single-line comments (// ...) which some models (like deepseek) hallucinate into JSON
    text = _strip_comments(text)
    return text.strip()


def is_quota_error(error_msg: str) -> bool:
    keywords = [
        "usage limit", "quota", "rate limit", "credits", 
        "may not exist", "access to it", "not have access",
        "model not found", "unauthorized", "overloaded",
        "not logged in", "please run /login",
        "invalid api key", "fix external api key", "api key",
        "anthropic_api_key", "connectors are disabled",
        "auth source is set", "authentication", "forbidden",
        "bad credentials", "auth error"
    ]
    return any(x in error_msg.lower() for x in keywords)


def is_unusable_model_error(error_msg: str) -> tuple[bool, bool]:
    """Return `(is_unusable, is_key_rejected)` for persistent auth/provider/missing-model failures (not transient rate limits)."""
    target = summarize_cli_error(error_msg) if "\n" in error_msg else error_msg
    low = target.lower()
    norm = low.replace("-", " ").replace("_", " ")
    key_markers = (
        "invalid api key", "fix external api key", "api key rejected", "missing api key", "unauthorized",
        "bad credentials", "auth error", "authentication failed", "failed to authenticate", "not logged in", "please run /login",
        "auth source is set", "not signed in", "oauth access token has expired", "token has expired", "re authenticate",
    )
    has_forbidden = "forbidden" in norm and not re.search(r"\braise\b|\bvalueerror\b", norm)
    if has_forbidden or any(m in norm for m in key_markers) or re.search(r"\b(?:401|403)\b", low):
        return True, True
    unavailable_markers = (
        "model not found", "unknown model", "may not exist", "does not exist",
        "not have access", "access to it", "not supported", "not available", "providermodelnotfounderror",
        "is deactivated", "model_deactivated", "deactivated",
    )
    if any(m in norm for m in unavailable_markers) or "model_not_found" in low:
        return True, False
    return False, False


HANDOFF = ("\n\n### HANDOFF ###\nAnother model ({model}) was doing this task and stopped partway because it ran out of its usage "
           "limit. Anything it changed is already in the files. Start by reading `git status` and `git diff`, keep what is right, "
           "and finish the task from where it stopped rather than starting over.\n")


def _out_of_usage(model_id: str) -> dict | None:
    try:
        from orchestrator.project_chat import out_of_usage
        return out_of_usage(model_id)
    except Exception:
        return None


JSON_REMINDER = (
    "\n\n### FORMAT CORRECTION ###\n"
    "Your previous reply was not a single JSON object. Reply again with ONLY the JSON object, as plain text in your message. "
    "Do not write it to a file, do not use tools, and do not add commentary before or after it."
)


def _is_free_only_allowlist(allowed_models: list[str] | None) -> bool:
    """True when `allowed_models` is non-empty and every resolvable model in it is free (`cost_factor == 0.0`)."""
    if not allowed_models:
        return False
    metas = [m for mid in allowed_models if (m := get_model(mid))]
    return bool(metas) and all(m.cost_factor == 0.0 for m in metas)


def _cheapest_adequate_first(candidates: list[str], role: str | None, primary_id: str) -> list[str]:
    """Order sign-in fallbacks so the work moves to the cheapest model that is still up to it, instead of to the strongest.

    The router ranks by suitability, which puts the top tier first: when a Sonnet sign-in expired, the fallback was the
    Antigravity-hosted Opus (twice the price) ahead of its Sonnet. A model is adequate when it is no weaker than the one that
    failed and meets the role's own minimum tier (a verifier needs the top tier, a builder doesn't). Adequate models come
    first, cheapest first, and the router's order breaks ties; the rest follow in the router's order, in case nothing adequate
    works. The caller's "hosted Claude first" grouping is applied after this and keeps this order inside each group."""
    floor = ROLE_REQUIREMENTS.get(role or "", {}).get("min_tier")
    tiers = [int(floor)] if floor is not None else []
    failed = get_model(primary_id)
    if failed:
        tiers.append(int(failed.tier))
    if not tiers:
        return candidates
    bar = min(tiers)  # a smaller tier number is a stronger model

    def key(mid: str) -> tuple[bool, float]:
        meta = get_model(mid)
        if not meta or int(meta.tier) > bar:
            return (True, 0.0)
        return (False, meta.cost_factor)

    return sorted(candidates, key=key)  # stable: ties and the weaker models keep the router's order


def _auth_fallback_models(
    role: str | None,
    failed_clis: set[str],
    already_tried: set[str],
    primary_id: str,
    max_fallbacks: int = 5,
) -> list[str]:
    """Ready, signed-in models on this computer from OTHER providers when every allowed model's tool rejected auth."""
    try:
        from orchestrator.project_setup import models_ready_here
        ready_ids = set(models_ready_here(ROOT))
    except Exception:
        ready_ids = set()
    if not ready_ids:
        return []
    prioritized = get_prioritized_models(role=role, allowed_models=list(ready_ids))
    candidates: list[str] = []
    for mid in prioritized:
        if mid in already_tried:
            continue
        m = get_model(mid)
        if not m or not model_usable(m):
            continue
        if set(m.required_clis) & failed_clis:
            continue
        candidates.append(mid)
    if not candidates:
        return []
    candidates = _cheapest_adequate_first(candidates, role, primary_id)
    # If Claude CLI's sign-in expired, try Antigravity-hosted Claude models first before other providers.
    if "claude" in failed_clis or primary_id.startswith("claude-"):
        hosted_claude = [mid for mid in candidates if mid.startswith("claude-")]
        others = [mid for mid in candidates if not mid.startswith("claude-")]
        candidates = hosted_claude + others
    return candidates[:max_fallbacks]


def run_llm(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, allowed_models: list[str] | None = None, role: str | None = None, session_id: str | None = None, stream: bool = False, images: list[Path] | None = None, protected_paths: list[Path] | None = None, web: bool = False, raw: bool = False, on_chunk: Callable[[str | None], None] | None = None, *, job_type: str | None = None, job_id: str | None = None) -> tuple[str, str, str]:
    """`raw` returns the whole answer. Otherwise only its JSON part comes back, which cuts prose and links from a conversational answer."""
    # Resolve actual model ID if it's an alias or generic name
    resolved_model = get_model(model)
    primary_id = resolved_model.id if resolved_model else model

    if primary_id not in {m.id for m in get_all_models()}:
        raise ValueError(f"Unsupported model: {model}")

    # A model its provider can't run now would only fail first. Use the one the provider offers that stands in for it.
    substituted = False
    if resolved_model and not model_usable(resolved_model):
        stand_in = model_substitute(resolved_model)
        if stand_in and (not allowed_models or stand_in.id in allowed_models):
            print(f"ℹ️  {primary_id} isn't available to the {resolved_model.family} login on this computer. Using {stand_in.id} instead.")
            primary_id, resolved_model, substituted = stand_in.id, stand_in, True

    # Define fallback sequence based on the role and allowed models
    preferred_family = resolved_model.family if resolved_model else None
    
    attempts = get_prioritized_models(
        role=role, 
        allowed_models=allowed_models, 
        preferred_family=preferred_family
    )
    
    # Ensure the requested primary model is tried first
    if primary_id in attempts:
        attempts.remove(primary_id)
        attempts.insert(0, primary_id)
    elif not allowed_models or substituted:
        attempts.insert(0, primary_id)

    audit = None
    if job_type and role:
        try:
            ready = [candidate for candidate in attempts
                     if (meta := get_model(candidate)) and runnable_here(meta) and not _out_of_usage(candidate)]
        except Exception:
            ready = []  # Availability checks for shadow recommendations are advisory.
        audit = AttemptAudit(LOGS_DIR / "model-attempts.jsonl", role, job_type, ready,
                             job_id=job_id, requested_model=model)

    def report(result: str) -> None:
        if audit:
            audit.record(current_model, result, time.monotonic() - started, format_retry=format_retry)

    protection = {"protected_paths": protected_paths} if protected_paths else {}
    last_error = None
    actual_session_id = session_id or str(uuid.uuid4())

    handoff = ""  # set when a model ran out of usage partway: the next one is told the work is already started
    failed_auth_clis: set[str] = set()
    attempted_models: list[str] = []
    auth_failed_count = 0
    provider_blocked_count = 0
    expanded_auth_fallback = False
    idx = 0
    while True:
        if idx >= len(attempts):
            if (
                not expanded_auth_fallback
                and provider_blocked_count > 0
                and provider_blocked_count == len(attempted_models)
                and not _is_free_only_allowlist(allowed_models)
            ):
                expanded_auth_fallback = True
                extras = _auth_fallback_models(role, failed_auth_clis, set(attempts), primary_id)
                if extras:
                    if auth_failed_count > 0:
                        print(f"ℹ️  Primary tool sign-in expired or rejected; switching to ready signed-in AI model(s) on this computer: {', '.join(extras[:3])}...")
                    else:
                        print(f"ℹ️  Configured models are out of usage or unavailable; switching to ready signed-in AI model(s) on this computer: {', '.join(extras[:3])}...")
                    attempts.extend(extras)
                    continue
            break
        current_model = attempts[idx]
        idx += 1
        current_meta = get_model(current_model)
        if current_meta and failed_auth_clis and (set(current_meta.required_clis) & failed_auth_clis):
            print(f"⏭️  Skipping {current_model}: its tool's sign-in was rejected.")
            continue
        if len(attempted_models) > 0 and _out_of_usage(current_model):  # its tool ran out a moment ago, in this same call
            print(f"⏭️  Skipping {current_model}: {_out_of_usage(current_model).get('reason')}.")
            continue
        attempted_models.append(current_model)
        started = time.monotonic()
        format_retry = False
        fallback_note = "Attempting fallback..." if idx < len(attempts) else "No fallback models remain."
        try:
            attempt_prompt = prompt + handoff if handoff and role in (ModelRole.BUILDER, ModelRole.DEBUGGER) else prompt
            raw_output = _run_llm_single(current_model, attempt_prompt, cwd, timeout, role=role, session_id=session_id, images=images, on_chunk=on_chunk, **protection, **({"web": True} if web else {}), **({"raw": True} if raw else {}))
            
            # Validation: if role expects JSON, verify we have it but DO NOT overwrite raw_output
            if role in [ModelRole.PLANNER, ModelRole.BUILDER, ModelRole.DEBUGGER, ModelRole.VERIFIER]:
                try:
                    json.loads(extract_json_block(raw_output), strict=False)
                except json.JSONDecodeError as exc:
                    format_retry = True
                    # Agentic models often do the work, then write the answer to a file or describe it in prose.
                    # Ask the SAME model once to restate it as plain JSON before moving on to another model;
                    # with only one model allowed (a free-only setup) there is no other to move on to.
                    print(f"⚠️  {current_model} produced invalid JSON. Asking it to restate the answer as plain JSON...")
                    if on_chunk:
                        on_chunk(None)
                    try:
                        raw_output = _run_llm_single(current_model, prompt + JSON_REMINDER, cwd, timeout, role=role, session_id=session_id, images=images, on_chunk=on_chunk, **protection)
                        json.loads(extract_json_block(raw_output), strict=False)
                    except json.JSONDecodeError as exc2:
                        print(f"⚠️  {current_model} still produced invalid JSON. {fallback_note}")
                        # The headline comes first and starts with "Error:", which is the line summarize_cli_error picks. Otherwise
                        # the person is told only "Expecting value: line 1 column 1", with no model and no hint it was the answer's format.
                        last_error = RuntimeError(f"Error: {current_model} produced invalid JSON ({exc2.msg}).\n{raw_output}")
                        report("invalid_json")
                        if on_chunk:
                            on_chunk(None)
                        continue
            
            # Clear any prior recorded failure for this model now that it answered cleanly.
            try:
                from orchestrator.project_chat import load_checks, record_check
                if load_checks().get(current_model, {}).get("ok") is False:
                    record_check(current_model, True)
            except Exception:
                pass

            # Return the full raw output so callers can see research/thoughts
            report("response_accepted")
            if audit:
                audit.finish(current_model)
            return raw_output, current_model, actual_session_id
        except LLMTimeoutError as exc:
            report("timeout")
            if on_chunk:
                on_chunk(None)
            print(f"⚠️  {current_model} timed out. {fallback_note}")
            last_error = exc
            continue
        except RuntimeError as exc:
            if on_chunk:
                on_chunk(None)
            err_str = str(exc)
            summary = summarize_cli_error(err_str)
            if usage_limits.detect(err_str):
                report("quota")
                provider_blocked_count += 1
                # Its allowance is used up: left out (every job, this tool's whole family) until it resets, then back by itself.
                try:
                    from orchestrator.project_chat import record_out_of_usage
                    hold = record_out_of_usage(current_model, err_str)
                    print(f"⏸️  {current_model} is {hold['reason']}. It's left out until then and used again after. {fallback_note}")
                except Exception:
                    print(f"⏸️  {current_model} ran out of usage. {fallback_note}")
                handoff = HANDOFF.format(model=current_model)
                last_error = exc
                continue
            unusable, key_rejected = is_unusable_model_error(err_str)
            if unusable:
                provider_blocked_count += 1
                try:
                    from orchestrator.project_chat import record_model_failure
                    record_model_failure(current_model, summary, key_rejected=key_rejected)
                except Exception:
                    pass
                if key_rejected:
                    auth_failed_count += 1
                    if current_meta:
                        failed_auth_clis.update(current_meta.required_clis)
            report("auth" if key_rejected else "unavailable" if unusable else "execution_error")
            print(f"⚠️  {current_model} failed ({summary}). {fallback_note}")
            last_error = exc
            continue
        except Exception as exc:
            report("execution_error")
            if on_chunk:
                on_chunk(None)
            print(f"⚠️  {current_model} failed ({exc}). {fallback_note}")
            last_error = exc
            continue

    if audit:
        audit.finish(None)
    tried = ", ".join(attempted_models) or primary_id
    raise RuntimeError(f"No model could answer (tried: {tried}). Last error: {summarize_cli_error(str(last_error))}")


import selectors

def image_args(cmd_base: str, images: list[Path] | None) -> str:
    """Flags that let a model CLI see screenshots: attached where the CLI can attach images, otherwise their folder is
    opened to its file tools (the prompt lists the paths). CLIs without either (Ollama) get nothing and the prompt
    asks the model to say it couldn't look."""
    if not images:
        return ""
    files = [shlex.quote(str(p)) for p in images]
    folders = sorted({shlex.quote(str(Path(p).parent)) for p in images})
    program = cmd_base.split(" ", 1)[0]
    if program == "codex":
        return "".join(f" -i {f}" for f in files)
    if program == "opencode":
        return "".join(f" -f {f}" for f in files)
    if program in ("claude", "agy"):
        return "".join(f" --add-dir {d}" for d in folders)
    if " --prompt - " in cmd_base:  # the Gemini CLI
        return "".join(f" --include-directories {d}" for d in folders)
    return ""


def web_capable(model: str) -> bool:
    """Can this model look things up on the web when asked (`web=True`)? Claude Code and the Gemini CLI can; the others answer from memory."""
    m_meta = get_model(model)
    model_id = m_meta.id if m_meta else model
    if model_id.startswith("gemini-") or model_id == "gemini" or (m_meta and m_meta.family == "gemini"):
        return preferred_cli("gemini") != "agy"
    if model_id.startswith("claude-") or model_id == "claude":
        return True
    return False


def _claude_edit_mode(role: str | None) -> str:
    """Claude Code in print mode refuses file edits unless something allows them, so a builder or debugger would finish having
    written nothing. Those two roles may edit files (as the other CLIs' builders already can); every other role stays read-only."""
    return " --permission-mode acceptEdits" if role in (ModelRole.BUILDER, ModelRole.DEBUGGER) else ""


def get_llm_command(model: str, prompt_file: str, role: str | None = None, session_id: str | None = None, images: list[Path] | None = None, web: bool = False) -> str:
    # Resolve actual model ID
    m_meta = get_model(model)
    model_id = m_meta.id if m_meta else model

    # Use api_model_id if set (e.g. gpt-5.5-medium maps to gpt-5.5)
    cli_model_id = m_meta.api_model_id if m_meta and m_meta.api_model_id else model_id

    # Codex refuses to run outside a Git repository it trusts. A question asked from Ask AI or a job's chat runs in an empty
    # scratch folder on purpose (so the model can't change the project), so the check is skipped; inside a repository it
    # changes nothing.
    if model_id == "codex":
        cmd_base = "codex exec --skip-git-repo-check"
        if role in [ModelRole.BUILDER, ModelRole.DEBUGGER]:
            cmd_base += " --full-auto"
    elif model_id.startswith(("gpt-5", "gpt-6")) or (m_meta and m_meta.family == "openai" and "codex" in m_meta.required_clis):
        cmd_base = f"codex exec --model {cli_model_id}"
        if m_meta and m_meta.reasoning_effort:
            cmd_base += f" -c model_reasoning_effort={m_meta.reasoning_effort}"
        cmd_base += " --skip-git-repo-check"
        if role in [ModelRole.BUILDER, ModelRole.DEBUGGER]:
            cmd_base += " --full-auto"
    elif model_id.startswith("gemini-") or (m_meta and m_meta.family == "gemini"):
        provider_cli = preferred_cli("gemini")
        # Safe Agentic Planning: Allow read/search/docs servers for intelligence, 
        # but hide mutating/heavy servers (maestro, XcodeBuildMCP, ssh-manager, github) 
        # to prevent recursive loops and security blocks that cause hangs.
        safe_servers = "context7,exa,swiftlens"
        safe_tools = "read_file,grep_search,glob"
        if role in [ModelRole.BUILDER, ModelRole.DEBUGGER]:
            safe_tools += ",replace,write_file"
        if web:
            safe_tools += ",google_web_search,web_fetch"
        if provider_cli == "agy":
            cmd_base = f"agy --model {shlex.quote(cli_model_id)} --dangerously-skip-permissions"
            if session_id:
                cmd_base += f" --conversation {session_id}"
        else:
            cmd_base = f"{provider_cli} --model {shlex.quote(cli_model_id)} --skip-trust --prompt - --yolo --allowed-mcp-server-names {safe_servers} --allowed-tools {safe_tools} --raw-output --accept-raw-output-risk"
            if session_id:
                cmd_base += f" --session-id {session_id}"
    elif model_id == "gemini":
        provider_cli = preferred_cli("gemini")
        if provider_cli == "agy":
            cmd_base = "agy --dangerously-skip-permissions"
            if session_id:
                cmd_base += f" --conversation {session_id}"
        else:
            safe_servers = "context7,exa,swiftlens"
            safe_tools = "read_file,grep_search,glob"
            if role in [ModelRole.BUILDER, ModelRole.DEBUGGER]:
                safe_tools += ",replace,write_file"
            if web:
                safe_tools += ",google_web_search,web_fetch"
            cmd_base = f"{provider_cli} --skip-trust --prompt - --yolo --allowed-mcp-server-names {safe_servers} --allowed-tools {safe_tools} --raw-output --accept-raw-output-risk"
            if session_id:
                cmd_base += f" --session-id {session_id}"
    elif model_id.startswith("claude-"):
        cmd_base = f"claude -p --model {model_id}{_claude_edit_mode(role)}"
        if session_id:
            cmd_base += f" --session-id {session_id}"
        if web:
            cmd_base += ' --allowedTools "WebSearch,WebFetch"'
    elif model_id == "claude":
        cmd_base = f"claude -p --model claude-sonnet-4-6{_claude_edit_mode(role)}"
        if session_id:
            cmd_base += f" --session-id {session_id}"
        if web:
            cmd_base += ' --allowedTools "WebSearch,WebFetch"'
    elif model_id == "deepseek":
        cmd_base = _ollama_command("deepseek-coder", prompt_file)
    elif m_meta and ("ollama" in m_meta.required_clis or m_meta.family == "ollama" or m_meta.family == "qwen"):
        cmd_base = _ollama_command(model_id, prompt_file)
    elif model_id == "copilot":
        cmd_base = "gh copilot"
    # OpenCode: pass --auto so a headless job doesn't stall waiting for a human to approve a tool
    # call (the same auto-accept posture gemini/agy/pool already run with via --yolo/--unsafe-auto-allow).
    elif model_id.startswith("opencode/"):
        cmd_base = f"opencode run --model {model_id} --auto"
    elif model_id == "opencode":
        cmd_base = "opencode run --model opencode/big-pickle --auto"
    elif m_meta and m_meta.family == "opencode":
        cmd_base = f"opencode run --model opencode/{model_id.replace('opencode/', '')} --auto"
    elif m_meta and m_meta.family == "pool":
        cmd_base = "pool exec --unsafe-auto-allow -p -"
    elif model_id == "hermes" or (m_meta and m_meta.family == "hermes"):
        cmd_base = "hermes -z"
    else:
        cmd_base = f"{model_id}"

    cmd_base = _apply_yolo_choice(cmd_base)
    return f'cat "{shlex.quote(prompt_file)}" | {cmd_base}{image_args(cmd_base, images)}'


def _ollama_command(model: str, prompt_file: str) -> str:
    """`ollama run` for a prompt that fits Ollama's default window. A longer one would silently lose its beginning (for Ask AI, the
    whole project), so it goes through the API, which can ask for a bigger window (see orchestrator/ollama_api.py)."""
    from orchestrator import ollama_api
    try:
        size = os.path.getsize(prompt_file)
    except OSError:
        size = 0
    if size <= ollama_api.RUN_MAX_CHARS:
        return f"ollama run {model} --nowordwrap"
    return f"{shlex.quote(sys.executable)} -P -m orchestrator script ollama_generate.py --model {shlex.quote(model)}"


def _apply_yolo_choice(cmd_base: str) -> str:
    """Drop the tool's auto-approve flag when the owner turned YOLO off for it on the AI tools page."""
    from orchestrator.ai_providers import YOLO_FLAGS, yolo_on, without_yolo
    cli = cmd_base.split(" ", 1)[0]
    if cli not in YOLO_FLAGS:
        return cmd_base
    try:
        from common import read_json
        settings = read_json(CONFIG_DIR / "settings.json") if (CONFIG_DIR / "settings.json").exists() else {}
    except Exception:
        settings = {}
    return cmd_base if yolo_on(settings, cli) else without_yolo(cmd_base, cli)


def get_llm_env() -> dict[str, str]:
    """Returns the environment dictionary with common CLI paths added to PATH and keys from settings."""
    env = os.environ.copy()
    env["GEMINI_CLI_TRUST_WORKSPACE"] = "true"
    env["ANTIGRAVITY_CLI_TRUST_WORKSPACE"] = "true"
    env["AGY_CLI_TRUST_WORKSPACE"] = "true"
    
    # Load keys from settings.json as fallbacks
    try:
        from common import read_json
        settings_path = CONFIG_DIR / "settings.json"
        if settings_path.exists():
            settings = read_json(settings_path)
            
            # Map of setting_key -> env_var_name
            key_map = {
                "gemini_api_key": "GEMINI_API_KEY",
                "anthropic_api_key": "ANTHROPIC_API_KEY",
                "codex_api_key": "CODEX_API_KEY",
                "openai_api_key": "OPENAI_API_KEY",
                "ollama_api_key": "OLLAMA_API_KEY",
                "ollama_host": "OLLAMA_HOST"
            }
            
            for s_key, e_var in key_map.items():
                if e_var not in env and settings.get(s_key):
                    env[e_var] = settings[s_key]
    except:
        pass

    # Ensure common CLI paths are in PATH, especially for remote SSH workers
    paths = env.get("PATH", "").split(os.pathsep)
    extra_paths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
    ]
    for p in extra_paths:
        if p not in paths:
            paths.insert(0, p)
    env["PATH"] = os.pathsep.join(paths)
    return env


def _run_llm_single(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, role: str | None = None, session_id: str | None = None, images: list[Path] | None = None, protected_paths: list[Path] | None = None, web: bool = False, raw: bool = False, on_chunk: Callable[[str], None] | None = None) -> str:
    source = os.environ.get("AI_REQUEST_SOURCE")
    if source:
        prompt = f"[SOURCE: {source}]\n\n{prompt}"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8") as f:
        f.write(prompt)
        prompt_file = f.name

    actual_session_id = session_id or str(uuid.uuid4())
    cmd = get_llm_command(model, prompt_file, role=role, session_id=actual_session_id, images=images, web=web)
    if protected_paths:
        from orchestrator.job_scope import protect_command
        if "codex exec" in cmd:
            # Codex sandboxes its own shell commands, and macOS refuses a sandbox inside another, so it couldn't read the project at all.
            # The read-only sandbox around it is the one that protects the project, which is what this flag is for.
            cmd = cmd.replace("codex exec", "codex exec --dangerously-bypass-approvals-and-sandbox", 1)
        cmd = protect_command(cmd, protected_paths)
    env = get_llm_env()

    prompt_chars = len(prompt)
    print(f"      - Running LLM ({model}, timeout={timeout}s)...", flush=True)
    print(f"        [input] Sending {prompt_chars} chars of prompt context", flush=True)

    stdout_chunks = []
    stderr_chunks = []
    last_milestone = "Sending prompt"

    # Its own process group, so stopping a call stops the model tool too, not just the shell that started it.
    process = subprocess.Popen(
        cmd,
        cwd=str(cwd or ROOT),
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
    )

    def stop_call() -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            process.kill()

    sel = selectors.DefaultSelector()
    sel.register(process.stdout, selectors.EVENT_READ)
    sel.register(process.stderr, selectors.EVENT_READ)
    out_fd, err_fd = process.stdout.fileno(), process.stderr.fileno()
    decoders = {fd: codecs.getincrementaldecoder("utf-8")(errors="replace") for fd in (out_fd, err_fd)}
    partial = {out_fd: "", err_fd: ""}

    import time
    start_time = time.time()
    last_activity = start_time
    last_heartbeat = start_time
    exited_at = None
    is_dev_console = os.environ.get("AI_REQUEST_SOURCE") == "dev_console"

    # Initialize stylized progress indicator
    thinking_label = "Thinking"
    if prompt_chars > 100000:
        thinking_label = f"Deep Reasoning: Large Context ({prompt_chars} chars)... Please wait."
    elif model.startswith("gemini-3.1") or model.startswith("gpt-5.5") or model.startswith("claude-opus") or "qwen" in model.lower():
        thinking_label = "Thinking: Deep Reasoning model active"
    indicator = ProgressIndicator(label=thinking_label, hint="Ctrl-C to cancel")
    detector = LoopTroubleDetector(start_time=start_time)
    stderr_noise = [
        "Ripgrep is not available",
        "MCP issues detected",
        "Prompt with name",
        "Tool with name",
        "registered. Overwriting",
        "DeprecationWarning",
        "AssignCodingAgent",
        "issue_to_fix_workflow",
        "exec /bin/zsh",
        "succeeded in 0ms",
        "no such file or directory: /Users",
        "sed -n",
        "stat -f",
        "find ai/output",
        "tokens used",
        "Received",
        "chars in",
        "codex",
        "connectors are disabled",
        "conversation \""
    ]

    def handle_line(line: str, from_stdout: bool) -> None:
        nonlocal last_milestone
        line_notices = detector.record_line(line, now=time.time())
        if line_notices:
            indicator.clear()
            for notice in line_notices:
                print(f"\n{notice}\n", flush=True)
        if from_stdout:
            stdout_chunks.append(line)
            indicator.label = f"Generating ({len(stdout_chunks)} lines received)"
            if len(stdout_chunks) % 15 == 0:
                indicator.clear()
                last_milestone = f"Received {len(stdout_chunks)} lines"
                print(f"        [progress] received {len(stdout_chunks)} lines of response...", flush=True)
            return
        stderr_chunks.append(line)
        s_line = line.strip()
        if s_line and not any(n.lower() in s_line.lower() for n in stderr_noise):
            indicator.clear()
            print(f"        [stderr] {colorize_diff_line(s_line)}", flush=True)
            indicator.label = "Thinking (Active stderr)"

    def give_up(reason: str, limit: int) -> None:
        indicator.clear()
        stop_call()
        full_stdout = "".join(stdout_chunks)
        full_stderr = "".join(stderr_chunks)
        append_log(f"llm-{model}", (
            f"PROMPT:\n{prompt}\n\nMODEL: {model}\nCMD: {cmd}\nSESSION_ID: {actual_session_id}\nRETURN CODE: TIMEOUT\n\n"
            f"STDOUT:\n{full_stdout}\n\nSTDERR:\n{full_stderr}\n"))
        raise LLMTimeoutError(model=model, timeout=limit, duration=time.time() - start_time, input_chars=prompt_chars,
                              output_chars=len(full_stdout), last_milestone=f"{reason} (Last: {last_milestone})")

    # Two limits: `timeout` with no output at all, and a hard cap on the whole call (output such as tool chatter on stderr
    # resets the first, so on its own a call could run forever).
    idle_limit = timeout * 2 if is_dev_console else timeout
    hard_limit = idle_limit * 3

    try:
        while True:
            now = time.time()
            if now - last_activity > idle_limit:
                give_up(f"Stalled for {int(now - last_activity)}s", idle_limit)
            if now - start_time > hard_limit:
                give_up(f"Still running after {int((now - start_time) // 60)}m", hard_limit)

            # Check time-based loop/stall warnings
            time_notices = detector.check_time_triggers(now=now, last_output_time=last_activity)
            if time_notices:
                indicator.clear()
                for notice in time_notices:
                    print(f"\n{notice}\n", flush=True)

            indicator.render(last_activity_time=last_activity)

            # A line in the log every minute, so a long call visibly isn't frozen.
            if now - last_heartbeat >= 60:
                last_heartbeat = now
                indicator.clear()
                print(f"        [waiting] {model}: {int((now - start_time) // 60)}m so far, last output {int(now - last_activity)}s ago"
                      f" (gives up after {idle_limit // 60}m with no output, or {hard_limit // 60}m in all)", flush=True)

            # Read whatever is there without waiting for a full line: a tool that writes half a line and pauses
            # must not freeze this loop (and with it the timeouts above).
            for key, _ in sel.select(timeout=0.1):
                fd = key.fileobj.fileno()
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    sel.unregister(key.fileobj)
                    continue
                last_activity = time.time()
                decoded = decoders[fd].decode(data)
                if fd == out_fd and on_chunk and decoded:
                    on_chunk(decoded)
                text = partial[fd] + decoded
                *lines, partial[fd] = text.split("\n")
                for line in lines:
                    handle_line(line + "\n", fd == out_fd)

            if process.poll() is not None:
                exited_at = exited_at or time.time()
                # Done once both streams are closed (or shortly after exit, if something it started still holds them).
                if not sel.get_map() or time.time() - exited_at > 5:
                    break
        for fd in (out_fd, err_fd):
            final_decoded = decoders[fd].decode(b"", final=True)
            if fd == out_fd and on_chunk and final_decoded:
                on_chunk(final_decoded)
            rest = partial[fd] + final_decoded
            if rest:
                handle_line(rest, fd == out_fd)
    finally:
        indicator.clear()
        sel.close()
        if os.path.exists(prompt_file):
            os.remove(prompt_file)

    duration = time.time() - start_time
    full_stdout = "".join(stdout_chunks)
    full_stderr = "".join(stderr_chunks)
    
    print(f"        [output] Received {len(full_stdout)} chars in {duration:.1f}s")

    full_log = (
        f"PROMPT:\n{prompt}\n\n"
        f"MODEL: {model}\n"
        f"CMD: {cmd}\n"
        f"SESSION_ID: {actual_session_id}\n"
        f"RETURN CODE: {process.returncode}\n\n"
        f"STDOUT:\n{full_stdout}\n\n"
        f"STDERR:\n{full_stderr}\n"
    )
    append_log(f"llm-{model}", full_log)

    if process.returncode != 0:
        combined_output = f"{full_stdout}\n{full_stderr}".strip()
        raise RuntimeError(f"{model} failed (code {process.returncode}):\n{combined_output}")

    if raw:
        return re.sub(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])', '', full_stdout).strip()
    return extract_json_block(full_stdout)
