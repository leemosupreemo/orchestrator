#!/usr/bin/env python3
from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

from common import CONFIG_DIR, ROOT, append_log, ProgressIndicator, colorize_diff_line, LoopTroubleDetector
from model_registry import get_model, ModelTier, get_all_models, preferred_cli, summarize_cli_error, model_usable, model_substitute
from model_router import get_prioritized_models, ModelRole

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


JSON_REMINDER = (
    "\n\n### FORMAT CORRECTION ###\n"
    "Your previous reply was not a single JSON object. Reply again with ONLY the JSON object, as plain text in your message. "
    "Do not write it to a file, do not use tools, and do not add commentary before or after it."
)


def run_llm(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, allowed_models: list[str] | None = None, role: str | None = None, session_id: str | None = None, stream: bool = False, images: list[Path] | None = None, protected_paths: list[Path] | None = None, web: bool = False, raw: bool = False) -> tuple[str, str, str]:
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
        if stand_in:
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

    protection = {"protected_paths": protected_paths} if protected_paths else {}
    last_error = None
    actual_session_id = session_id or str(uuid.uuid4())

    for current_model in attempts:
        try:
            raw_output = _run_llm_single(current_model, prompt, cwd, timeout, role=role, session_id=session_id, images=images, **protection, **({"web": True} if web else {}), **({"raw": True} if raw else {}))
            
            # Validation: if role expects JSON, verify we have it but DO NOT overwrite raw_output
            if role in [ModelRole.PLANNER, ModelRole.BUILDER, ModelRole.DEBUGGER, ModelRole.VERIFIER]:
                try:
                    json.loads(extract_json_block(raw_output), strict=False)
                except json.JSONDecodeError as exc:
                    # Agentic models often do the work, then write the answer to a file or describe it in prose.
                    # Ask the SAME model once to restate it as plain JSON before moving on to another model;
                    # with only one model allowed (a free-only setup) there is no other to move on to.
                    print(f"⚠️  {current_model} produced invalid JSON. Asking it to restate the answer as plain JSON...")
                    try:
                        raw_output = _run_llm_single(current_model, prompt + JSON_REMINDER, cwd, timeout, role=role, session_id=session_id, images=images, **protection)
                        json.loads(extract_json_block(raw_output), strict=False)
                    except json.JSONDecodeError as exc2:
                        print(f"⚠️  {current_model} still produced invalid JSON. Attempting fallback...")
                        last_error = RuntimeError(f"{current_model} produced invalid JSON:\n{raw_output}\nError: {exc2}")
                        continue
            
            # Return the full raw output so callers can see research/thoughts
            return raw_output, current_model, actual_session_id
        except LLMTimeoutError as exc:
            # ... (timeout logging)
            print(f"⚠️  {current_model} timed out. Attempting fallback...")
            last_error = exc
            continue
        except RuntimeError as exc:
            err_str = str(exc)
            summary = summarize_cli_error(err_str)
            print(f"⚠️  {current_model} failed ({summary}). Attempting fallback...")
            last_error = exc
            continue
        except Exception as exc:
            print(f"⚠️  {current_model} failed ({exc}). Attempting fallback...")
            last_error = exc
            continue

    raise RuntimeError(f"All models failed (quota limits reached). Last error: {last_error}")


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
    if model_id.startswith("claude-") or model_id == "claude":
        return True
    if model_id.startswith("gemini-") or model_id == "gemini" or (m_meta and m_meta.family == "gemini"):
        return preferred_cli("gemini") != "agy"
    return False


def get_llm_command(model: str, prompt_file: str, role: str | None = None, session_id: str | None = None, images: list[Path] | None = None, web: bool = False) -> str:
    # Resolve actual model ID
    m_meta = get_model(model)
    model_id = m_meta.id if m_meta else model

    # Use api_model_id if set (e.g. gpt-5.5-medium maps to gpt-5.5)
    cli_model_id = m_meta.api_model_id if m_meta and m_meta.api_model_id else model_id

    if model_id == "codex":
        cmd_base = "codex exec"
    elif model_id.startswith(("gpt-5", "gpt-6")) or (m_meta and m_meta.family == "openai" and "codex" in m_meta.required_clis):
        cmd_base = f"codex exec --model {cli_model_id}"
        if m_meta and m_meta.reasoning_effort:
            cmd_base += f" -c model_reasoning_effort={m_meta.reasoning_effort}"
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
        cmd_base = f"claude -p --model {model_id}"
        if session_id:
            cmd_base += f" --session-id {session_id}"
        if web:
            cmd_base += ' --allowedTools "WebSearch,WebFetch"'
    elif model_id == "claude":
        cmd_base = "claude -p --model claude-sonnet-4-6"
        if session_id:
            cmd_base += f" --session-id {session_id}"
        if web:
            cmd_base += ' --allowedTools "WebSearch,WebFetch"'
    elif model_id == "deepseek":
        cmd_base = "ollama run deepseek-coder --nowordwrap"
    elif m_meta and ("ollama" in m_meta.required_clis or m_meta.family == "ollama" or m_meta.family == "qwen"):
        cmd_base = f"ollama run {model_id} --nowordwrap"
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


def _run_llm_single(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, role: str | None = None, session_id: str | None = None, images: list[Path] | None = None, protected_paths: list[Path] | None = None, web: bool = False, raw: bool = False) -> str:
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
                text = partial[fd] + decoders[fd].decode(data)
                *lines, partial[fd] = text.split("\n")
                for line in lines:
                    handle_line(line + "\n", fd == out_fd)

            if process.poll() is not None:
                exited_at = exited_at or time.time()
                # Done once both streams are closed (or shortly after exit, if something it started still holds them).
                if not sel.get_map() or time.time() - exited_at > 5:
                    break
        for fd in (out_fd, err_fd):
            rest = partial[fd] + decoders[fd].decode(b"", final=True)
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
