#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path

from common import CONFIG_DIR, ROOT, append_log, ProgressIndicator, colorize_diff_line, LoopTroubleDetector
from model_registry import get_model, ModelTier, get_all_models, preferred_cli, summarize_cli_error
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
    text = text.strip()

    # 1. Try markdown fences
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    else:
        # 2. Stack-based extraction for the first full object or array
        # This handles cases where models append garbage or closing braces after the valid JSON
        first_brace = text.find('{')
        first_bracket = text.find('[')
        
        start_idx = -1
        if first_brace != -1 and (first_bracket == -1 or first_brace < first_bracket):
            start_idx = first_brace
            opener, closer = '{', '}'
        elif first_bracket != -1:
            start_idx = first_bracket
            opener, closer = '[', ']'
        
        if start_idx != -1:
            stack = 0
            in_string = False
            escape = False
            for i in range(start_idx, len(text)):
                c = text[i]
                if c == '"' and not escape:
                    in_string = not in_string
                elif not in_string:
                    if c == opener:
                        stack += 1
                    elif c == closer:
                        stack -= 1
                        if stack == 0:
                            text = text[start_idx:i+1].strip()
                            break
                if c == '\\' and not escape:
                    escape = True
                else:
                    escape = False

    # 3. Strip single-line comments (// ...) which some models (like deepseek) hallucinate into JSON
    # This is non-standard JSON but common in LLM "pseudo-JSON"
    text = re.sub(r"//.*$", "", text, flags=re.M)
    
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


def run_llm(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, allowed_models: list[str] | None = None, role: str | None = None, session_id: str | None = None, stream: bool = False) -> tuple[str, str, str]:
    # Resolve actual model ID if it's an alias or generic name
    resolved_model = get_model(model)
    primary_id = resolved_model.id if resolved_model else model

    if primary_id not in {m.id for m in get_all_models()}:
        raise ValueError(f"Unsupported model: {model}")

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
    elif not allowed_models:
        attempts.insert(0, primary_id)

    last_error = None
    actual_session_id = session_id or str(uuid.uuid4())

    for current_model in attempts:
        try:
            raw_output = _run_llm_single(current_model, prompt, cwd, timeout, role=role, session_id=session_id)
            
            # Validation: if role expects JSON, verify we have it but DO NOT overwrite raw_output
            if role in [ModelRole.PLANNER, ModelRole.BUILDER, ModelRole.DEBUGGER, ModelRole.VERIFIER]:
                json_part = extract_json_block(raw_output)
                try:
                    json.loads(json_part)
                except json.JSONDecodeError as exc:
                    print(f"⚠️  {current_model} produced invalid JSON. Attempting fallback...")
                    last_error = RuntimeError(f"{current_model} produced invalid JSON:\n{raw_output}\nError: {exc}")
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

def get_llm_command(model: str, prompt_file: str, role: str | None = None, session_id: str | None = None) -> str:
    # Resolve actual model ID
    m_meta = get_model(model)
    model_id = m_meta.id if m_meta else model

    # Use api_model_id if set (e.g. gpt-5.5-medium maps to gpt-5.5)
    cli_model_id = m_meta.api_model_id if m_meta and m_meta.api_model_id else model_id

    if model_id == "codex":
        cmd_base = "codex exec"
    elif model_id.startswith("gpt-5"):
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
            cmd_base = f"{provider_cli} --skip-trust --prompt - --yolo --allowed-mcp-server-names {safe_servers} --allowed-tools {safe_tools} --raw-output --accept-raw-output-risk"
            if session_id:
                cmd_base += f" --session-id {session_id}"
    elif model_id.startswith("claude-"):
        cmd_base = f"claude -p --model {model_id}"
        if session_id:
            cmd_base += f" --session-id {session_id}"
    elif model_id == "claude":
        cmd_base = "claude -p --model claude-sonnet-4-6"
        if session_id:
            cmd_base += f" --session-id {session_id}"
    elif model_id == "deepseek":
        cmd_base = "ollama run deepseek-coder"
    elif m_meta and ("ollama" in m_meta.required_clis or m_meta.family == "ollama" or m_meta.family == "qwen"):
        cmd_base = f"ollama run {model_id}"
    elif model_id == "copilot":
        cmd_base = "gh copilot"
    elif model_id.startswith("opencode/"):
        cmd_base = f"opencode run --model {model_id}"
    elif model_id == "opencode":
        cmd_base = "opencode run --model opencode/big-pickle"
    elif m_meta and m_meta.family == "opencode":
        cmd_base = f"opencode run --model opencode/{model_id.replace('opencode/', '')}"
    else:
        cmd_base = f"{model_id}"

    return f'cat "{shlex.quote(prompt_file)}" | {cmd_base}'


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


def _run_llm_single(model: str, prompt: str, cwd: Path | None = None, timeout: int = 300, role: str | None = None, session_id: str | None = None) -> str:
    source = os.environ.get("AI_REQUEST_SOURCE")
    if source:
        prompt = f"[SOURCE: {source}]\n\n{prompt}"

    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".md", encoding="utf-8") as f:
        f.write(prompt)
        prompt_file = f.name

    actual_session_id = session_id or str(uuid.uuid4())
    cmd = get_llm_command(model, prompt_file, role=role, session_id=actual_session_id)
    env = get_llm_env()

    prompt_chars = len(prompt)
    print(f"      - Running LLM ({model}, timeout={timeout}s)...", flush=True)
    print(f"        [input] Sending {prompt_chars} chars of prompt context", flush=True)

    stdout_chunks = []
    stderr_chunks = []
    last_milestone = "Sending prompt"

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd or ROOT),
        shell=True,
        executable="/bin/bash",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1, # Line buffered
    )

    sel = selectors.DefaultSelector()
    sel.register(process.stdout, selectors.EVENT_READ)
    sel.register(process.stderr, selectors.EVENT_READ)

    import time
    start_time = time.time()
    last_activity = start_time
    is_dev_console = os.environ.get("AI_REQUEST_SOURCE") == "dev_console"

    # Initialize stylized progress indicator
    thinking_label = "Thinking"
    if prompt_chars > 100000:
        thinking_label = f"Deep Reasoning: Large Context ({prompt_chars} chars)... Please wait."
    elif model.startswith("gemini-3.1") or model.startswith("gpt-5.5") or model.startswith("claude-opus") or "qwen" in model.lower():
        thinking_label = "Thinking: Deep Reasoning model active"
    indicator = ProgressIndicator(label=thinking_label, hint="Ctrl-C to cancel")
    detector = LoopTroubleDetector(start_time=start_time)

    try:
        while True:
            now = time.time()
            
            # For dev_console, we are more lenient with idle timeouts as users are often watching
            # and thinking models can have long pauses.
            idle_limit = timeout * 2 if is_dev_console else timeout
            
            # Timeout only triggers if NO output has been received for the timeout duration
            if now - last_activity > idle_limit:
                indicator.clear()
                process.kill()
                output_so_far = "".join(stdout_chunks)
                
                # Log even on timeout
                full_stdout = "".join(stdout_chunks)
                full_stderr = "".join(stderr_chunks)
                full_log = (
                    f"PROMPT:\n{prompt}\n\n"
                    f"MODEL: {model}\n"
                    f"CMD: {cmd}\n"
                    f"SESSION_ID: {actual_session_id}\n"
                    f"RETURN CODE: TIMEOUT\n\n"
                    f"STDOUT:\n{full_stdout}\n\n"
                    f"STDERR:\n{full_stderr}\n"
                )
                append_log(f"llm-{model}", full_log)
                
                raise LLMTimeoutError(
                    model=model,
                    timeout=idle_limit,
                    duration=now - start_time,
                    input_chars=prompt_chars,
                    output_chars=len(output_so_far),
                    last_milestone=f"Stalled for {int(now - last_activity)}s (Last: {last_milestone})"
                )

            # Check time-based loop/stall warnings
            time_notices = detector.check_time_triggers(now=now, last_output_time=last_activity)
            if time_notices:
                indicator.clear()
                for notice in time_notices:
                    print(f"\n{notice}\n", flush=True)

            # Update stylized indicator
            indicator.render(last_activity_time=last_activity)
            
            # Print periodic warning if stuck
            idle_sec = int(now - last_activity)
            if idle_sec > 0 and idle_sec % 240 == 0:
                print(f"\n      💡 NOTICE: {model} is thinking deeply ({idle_sec // 60}m so far).")
                print(f"         Reasoning models can sometimes take 5-10 minutes for complex tasks.")
                print(f"         Context: {prompt_chars} chars sent. Checking for progress...")

            for key, _ in sel.select(timeout=0.1): # Frequent polling for smooth spinner
                line = key.fileobj.readline()
                if not line:
                    continue
                
                # Reset activity timer on ANY output (stdout or stderr)
                last_activity = time.time()
                
                # Check line for loop / trouble indicators
                line_notices = detector.record_line(line, now=now)
                if line_notices:
                    indicator.clear()
                    for notice in line_notices:
                        print(f"\n{notice}\n", flush=True)
                
                if key.fileobj is process.stdout:
                    stdout_chunks.append(line)
                    # When receiving output, we update the label to show progress
                    indicator.label = f"Generating ({len(stdout_chunks)} lines received)"
                    if len(stdout_chunks) % 15 == 0:
                        indicator.clear()
                        last_milestone = f"Received {len(stdout_chunks)} lines"
                        print(f"        [progress] received {len(stdout_chunks)} lines of response...", flush=True)
                else:
                    stderr_chunks.append(line)
                    # Filter out common noise
                    s_line = line.strip()
                    noise = [
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
                    if not any(n.lower() in s_line.lower() for n in noise):
                        indicator.clear()
                        print(f"        [stderr] {colorize_diff_line(s_line)}", flush=True)
                        indicator.label = "Thinking (Active stderr)"

            if process.poll() is not None:
                # Process finished, read remaining output
                for line in process.stdout:
                    stdout_chunks.append(line)
                for line in process.stderr:
                    stderr_chunks.append(line)
                    indicator.clear()
                    print(f"        [stderr] {colorize_diff_line(line.strip())}", flush=True)
                break
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

    return extract_json_block(full_stdout)
