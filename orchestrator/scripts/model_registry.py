#!/usr/bin/env python3
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any
import re
import shutil
import subprocess
import time

# We use IntEnum to allow comparison (tier 1 < tier 2)
class ModelTier(IntEnum):
    EXTREME = 1  # Opus / O1
    HIGH = 2     # Pro / Sonnet
    MEDIUM = 3   # Flash / Haiku
    LOW = 4      # Lite / Small

class ModelCapability(Enum):
    REASONING = "reasoning"
    CODING = "coding"
    SPEED = "speed"
    CONTEXT = "context"
    VISION = "vision"

@dataclass
class ModelMetadata:
    id: str
    family: str
    tier: ModelTier
    capabilities: list[ModelCapability] = field(default_factory=list)
    cost_factor: float = 1.0
    aliases: list[str] = field(default_factory=list)
    required_clis: list[str] = field(default_factory=list)
    api_model_id: str | None = None
    reasoning_effort: str | None = None

@dataclass
class DiscoveryResult:
    models: list[ModelMetadata]
    details: list[str] = field(default_factory=list)

MODELS = [
    ModelMetadata(
        id="claude-opus-4-8",
        family="claude",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=10.0,
        required_clis=["claude"]
    ),
    ModelMetadata(
        id="claude-opus-4-7",
        family="claude",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=10.0,
        required_clis=["claude"]
    ),
    ModelMetadata(
        id="claude-sonnet-4-6",
        family="claude",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED, ModelCapability.CONTEXT],
        cost_factor=5.0,
        aliases=["claude"],
        required_clis=["claude"]
    ),
    ModelMetadata(
        id="claude-haiku-4-5",
        family="claude",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.SPEED],
        cost_factor=1.0,
        required_clis=["claude"]
    ),
    ModelMetadata(
        id="gemini-3.1-pro-preview",
        family="gemini",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT, ModelCapability.VISION],
        cost_factor=5.0,
        api_model_id="Gemini 3.1 Pro (High)",
        aliases=["gemini", "antigravity", "agy"],
        required_clis=["gemini"]
    ),
    ModelMetadata(
        id="gemini-3-flash-preview",
        family="gemini",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.SPEED, ModelCapability.CONTEXT, ModelCapability.VISION],
        cost_factor=1.0,
        api_model_id="Gemini 3.6 Flash (High)",
        required_clis=["gemini"]
    ),
    ModelMetadata(
        id="gemini-3.1-flash-lite-preview",
        family="gemini",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.SPEED, ModelCapability.CONTEXT],
        cost_factor=0.5,
        api_model_id="Gemini 3.6 Flash (Medium)",
        required_clis=["gemini"]
    ),
    ModelMetadata(
        id="gpt-5.5",
        family="openai",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=10.0,
        reasoning_effort="high",
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.5-high",
        family="openai",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=10.0,
        api_model_id="gpt-5.5",
        reasoning_effort="high",
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.5-medium",
        family="openai",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=5.0,
        api_model_id="gpt-5.5",
        reasoning_effort="medium",
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.5-low",
        family="openai",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=2.0,
        api_model_id="gpt-5.5",
        reasoning_effort="low",
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.4",
        family="openai",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.REASONING, ModelCapability.SPEED],
        cost_factor=4.0,
        aliases=["codex"],
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.4-mini",
        family="openai",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.SPEED, ModelCapability.CODING],
        cost_factor=1.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.3-codex",
        family="openai",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=2.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-5.2",
        family="openai",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.REASONING, ModelCapability.SPEED],
        cost_factor=3.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-4.5-preview",
        family="openai",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.CONTEXT],
        cost_factor=8.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-4o",
        family="openai",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED, ModelCapability.VISION],
        cost_factor=3.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="gpt-4o-mini",
        family="openai",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.5,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="o1",
        family="openai",
        tier=ModelTier.EXTREME,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING],
        cost_factor=10.0,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="o1-mini",
        family="openai",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.REASONING, ModelCapability.SPEED],
        cost_factor=1.5,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="o3-mini",
        family="openai",
        tier=ModelTier.MEDIUM,
        capabilities=[ModelCapability.REASONING, ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=1.5,
        required_clis=["codex"]
    ),
    ModelMetadata(
        id="deepseek",
        family="ollama",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.REASONING],
        cost_factor=0.0,
        required_clis=["ollama"]
    ),
    ModelMetadata(
        id="copilot",
        family="copilot",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.REASONING, ModelCapability.SPEED],
        cost_factor=0.0,
        required_clis=["gh"]
    ),
    ModelMetadata(
        id="opencode/big-pickle",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["opencode", "big-pickle"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/deepseek-v4-flash-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["deepseek-v4-flash-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/laguna-s-2.1-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["laguna-s-2.1-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/ling-3.0-flash-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["ling-3.0-flash-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/longcat-2.0-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["longcat-2.0-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/mimo-v2.5-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["mimo-v2.5-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/minimax-m2.5-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["minimax-m2.5-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/nemotron-3-super-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["nemotron-3-super-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/nemotron-3-ultra-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["nemotron-3-ultra-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/north-mini-code-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["north-mini-code-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="opencode/qwen3.6-plus-free",
        family="opencode",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["qwen3.6-plus-free"],
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="qwen2.5-coder",
        family="qwen",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["qwen", "qwen-coder"],
        required_clis=["ollama"]
    ),
    ModelMetadata(
        id="qwen-3.7-max",
        family="qwen",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.REASONING],
        cost_factor=0.0,
        required_clis=["ollama"]
    ),
    ModelMetadata(
        id="opencode/qwen-3.7-max",
        family="opencode",
        tier=ModelTier.HIGH,
        capabilities=[ModelCapability.CODING, ModelCapability.REASONING],
        cost_factor=0.0,
        required_clis=["opencode"]
    ),
    ModelMetadata(
        id="pool",
        family="pool",
        tier=ModelTier.LOW,
        capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
        cost_factor=0.0,
        aliases=["poolside"],
        required_clis=["pool"]
    ),
]

import json
import os
from pathlib import Path

# Legacy / Base IDs from llm.py that we should support as aliases or direct lookups
LEGACY_IDS = {
    "gemini": "gemini-3.1-pro-preview",
    "antigravity": "gemini-3.1-pro-preview",
    "agy": "gemini-3.1-pro-preview",
    "claude": "claude-sonnet-4-6",
    "codex": "gpt-5.4",
    "opencode": "opencode/big-pickle",
    "qwen": "qwen2.5-coder",
    "qwen-coder": "qwen2.5-coder",
}

CLI_ALIASES = {
    "gemini": ("agy", "antigravity", "gemini"),
    "qwen": ("ollama", "opencode", "qwen"),
}

def model_from_dict(item: dict[str, Any]) -> ModelMetadata:
    tier_str = item.get("tier", "HIGH").upper()
    tier = getattr(ModelTier, tier_str, ModelTier.HIGH)

    caps = []
    for c in item.get("capabilities", ["coding", "reasoning"]):
        try:
            caps.append(ModelCapability(c.lower()))
        except ValueError:
            pass

    return ModelMetadata(
        id=item["id"],
        family=item.get("family", "custom"),
        tier=tier,
        capabilities=caps,
        cost_factor=item.get("cost_factor", 1.0),
        aliases=item.get("aliases", []),
        required_clis=item.get("required_clis", []),
        api_model_id=item.get("api_model_id"),
        reasoning_effort=item.get("reasoning_effort")
    )

def load_bundled_models() -> list[ModelMetadata]:
    """Loads the package-owned registry shipped with the installed orchestrator."""
    bundled_path = Path(__file__).resolve().parents[1] / "config" / "models.json"
    try:
        data = json.loads(bundled_path.read_text(encoding="utf-8"))
        return [model_from_dict(item) for item in data.get("models", [])]
    except Exception as e:
        print(f"Warning: Failed to load bundled model registry from {bundled_path}: {e}")
        return []

def cli_is_available(cli_name: str, installed: dict[str, bool] | None = None) -> bool:
    """Returns whether a CLI is installed, honoring provider-specific aliases."""
    candidates = CLI_ALIASES.get(cli_name, (cli_name,))
    if installed is not None:
        return any(installed.get(candidate, False) for candidate in candidates)
    return any(shutil.which(candidate) is not None for candidate in candidates)

def preferred_cli(cli_name: str, installed: dict[str, bool] | None = None) -> str:
    """Returns the preferred installed CLI name for a provider."""
    candidates = CLI_ALIASES.get(cli_name, (cli_name,))
    if installed is not None:
        for candidate in candidates:
            if installed.get(candidate, False):
                return candidate
        return candidates[0]
    for candidate in candidates:
        if shutil.which(candidate) is not None:
            return candidate
    return candidates[0]

def load_custom_models() -> list[ModelMetadata]:
    """Loads custom models from ~/.orchestrator/custom_models.json or project config."""
    custom_models = []
    
    # Check global config
    global_config = Path.home() / ".orchestrator" / "custom_models.json"
    
    # Check project config if ORCHESTRATOR_PROJECT_ROOT is set
    project_config = None
    if "ORCHESTRATOR_PROJECT_ROOT" in os.environ:
        project_config = Path(os.environ["ORCHESTRATOR_PROJECT_ROOT"]) / ".orchestrator" / "config" / "custom_models.json"
        
    for config_path in [global_config, project_config]:
        if config_path and config_path.exists():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                for item in data.get("models", []):
                    custom_models.append(model_from_dict(item))
            except Exception as e:
                print(f"Warning: Failed to load custom models from {config_path}: {e}")
                
    return custom_models

# --------------------------------------------------------------------------- the Codex login
# Codex signed in with a ChatGPT account can only use the models OpenAI lists for that plan, and the Codex CLI keeps that list
# (models_cache.json) up to date itself. Read it as it is, so the models offered follow the login without a manual refresh.

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

_CODEX_MEMO: tuple | None = None

def codex_login_models() -> list[dict[str, Any]] | None:
    """The models the signed-in Codex CLI offers, best first, or None when it isn't using a ChatGPT login (an API key can use
    any model) or has no list yet."""
    global _CODEX_MEMO
    home = codex_home()
    key = tuple((home / name).stat().st_mtime_ns if (home / name).exists() else 0 for name in ("auth.json", "models_cache.json")) + (str(home),)
    if _CODEX_MEMO and _CODEX_MEMO[0] == key:  # asked once per model by the router and the page: read the files only when they change
        return _CODEX_MEMO[1]
    try:
        if json.loads((home / "auth.json").read_text(encoding="utf-8")).get("auth_mode") != "chatgpt":
            result = None
        else:
            models = json.loads((home / "models_cache.json").read_text(encoding="utf-8")).get("models")
            listed = [m for m in models or [] if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"]
            result = sorted(listed, key=lambda m: m.get("priority") if isinstance(m.get("priority"), (int, float)) else 999) or None
    except (OSError, ValueError, AttributeError):
        result = None
    _CODEX_MEMO = (key, result)
    return result

def codex_default_model() -> str:
    """The model the person chose in Codex's own config, if any."""
    try:
        import tomllib
        return str(tomllib.loads((codex_home() / "config.toml").read_text(encoding="utf-8")).get("model") or "")
    except (OSError, ValueError):
        return ""

def _codex_tier(entry: dict[str, Any]) -> ModelTier:
    text = str(entry.get("description") or "").lower()
    if "frontier" in text:
        return ModelTier.EXTREME
    if "workhorse" in text:
        return ModelTier.HIGH
    if any(word in text for word in ("affordable", "fast", "efficient", "straightforward", "balanced")):
        return ModelTier.MEDIUM
    return ModelTier.HIGH

def codex_login_metadata() -> list[ModelMetadata]:
    out = []
    for entry in codex_login_models() or []:
        tier = _codex_tier(entry)
        out.append(ModelMetadata(
            id=str(entry["slug"]), family="openai", tier=tier,
            capabilities=[ModelCapability.CODING, ModelCapability.REASONING, ModelCapability.CONTEXT],
            cost_factor={ModelTier.EXTREME: 10.0, ModelTier.HIGH: 5.0, ModelTier.MEDIUM: 2.0, ModelTier.LOW: 1.0}[tier],
            required_clis=["codex"]))
    return out

def is_codex_model(m: ModelMetadata) -> bool:
    return m.family == "openai" and "codex" in m.required_clis

def codex_usable(m: ModelMetadata) -> bool:
    """False for an OpenAI model this Codex login can't run (it would only fail and fall back)."""
    if not is_codex_model(m):
        return True
    offered = codex_login_models()
    return offered is None or (m.api_model_id or m.id) in {e["slug"] for e in offered}

def codex_substitute(m: ModelMetadata) -> ModelMetadata | None:
    """The model this Codex login offers that best stands in for `m`: the same standing if there is one, preferring the person's
    own Codex default, otherwise the closest."""
    usable = codex_login_metadata()
    if not usable:
        return None
    default = codex_default_model()
    return min(usable, key=lambda c: (abs(int(c.tier) - int(m.tier)), c.id != default))

_ALL_MODELS_CACHE = None

def get_all_models() -> list[ModelMetadata]:
    global _ALL_MODELS_CACHE
    if _ALL_MODELS_CACHE is None:
        bundled_models = load_bundled_models()
        merged = {m.id: m for m in (bundled_models or MODELS)}
        for m in load_custom_models():
            merged[m.id] = m
        for m in provider_login_metadata():
            merged.setdefault(m.id, m)
        _ALL_MODELS_CACHE = list(merged.values())
    return _ALL_MODELS_CACHE

# --------------------------------------------------------------------------- the other providers
# Each CLI says which models it can run: `agy models`, `opencode models`, Ollama's local list, and Claude Code's own record of the
# models it offered and the ones this account has used. They are fetched in the background (see start_provider_refresher) into one
# small file, and read from there, so building the registry never waits on a command.

PROVIDER_TTL = 3 * 3600
_PROVIDER_MEMO: tuple | None = None

def _state_dir() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_USER_STATE_DIR") or Path.home() / ".orchestrator")

def provider_cache_path() -> Path:
    return _state_dir() / "provider_models.json"

def read_provider_cache() -> dict[str, Any]:
    global _PROVIDER_MEMO
    path = provider_cache_path()
    key = (str(path), path.stat().st_mtime_ns if path.exists() else 0)
    if _PROVIDER_MEMO and _PROVIDER_MEMO[0] == key:
        return _PROVIDER_MEMO[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    _PROVIDER_MEMO = (key, data)
    return data

def refresh_provider_models(timeout: int = 15) -> dict[str, Any]:
    """Ask each installed provider CLI what it offers, and keep the answers. A provider that can't be reached keeps its last answer."""
    import urllib.request
    data = dict(read_provider_cache())
    agy = preferred_cli("gemini")
    if agy in {"agy", "antigravity"} and shutil.which(agy):
        try:
            res = subprocess.run([agy, "models"], capture_output=True, text=True, timeout=timeout)
            found = [{"slug": slug, "name": name} for slug, name in parse_agy_models_with_names(res.stdout) if slug.startswith("gemini")]
            if res.returncode == 0 and found:
                data["agy"] = found
        except Exception:
            pass
    if shutil.which("opencode"):
        try:
            res = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=timeout)
            found = parse_opencode_models_output(res.stdout)
            if res.returncode == 0 and found:
                data["opencode"] = found
        except Exception:
            pass
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
            data["ollama"] = [str(m["name"]) for m in json.loads(resp.read()).get("models", []) if m.get("name")]
    except Exception:
        pass
    data["fetched_at"] = time.time()
    path = provider_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data

def provider_cache_age() -> float | None:
    fetched = read_provider_cache().get("fetched_at")
    return time.time() - fetched if isinstance(fetched, (int, float)) else None

_REFRESHER = None

def start_provider_refresher(interval: int = PROVIDER_TTL // 2) -> None:
    """Keep the provider answers fresh while the web server runs: now if they are missing or old, then every `interval` seconds."""
    import threading
    global _REFRESHER
    if _REFRESHER:
        return
    def loop() -> None:
        while True:
            age = provider_cache_age()
            if age is None or age > PROVIDER_TTL:
                try:
                    refresh_provider_models()
                except Exception:
                    pass
            time.sleep(interval)
    _REFRESHER = threading.Thread(target=loop, name="provider-models", daemon=True)
    _REFRESHER.start()

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())

def _agy_tier(slug: str) -> ModelTier:
    base = ModelTier.MEDIUM if "flash" in slug else ModelTier.HIGH
    return ModelTier(min(int(base) + 1, int(ModelTier.LOW))) if slug.endswith("-low") else base

def agy_models_metadata() -> list[ModelMetadata]:
    """The Gemini models Antigravity offers, run by their display names (what `agy --model` is given)."""
    out = []
    for entry in read_provider_cache().get("agy") or []:
        tier = _agy_tier(entry["slug"])
        out.append(ModelMetadata(id=entry["slug"], family="gemini", tier=tier,
                                 capabilities=[ModelCapability.CODING, ModelCapability.REASONING, ModelCapability.CONTEXT, ModelCapability.VISION],
                                 cost_factor={ModelTier.HIGH: 5.0, ModelTier.MEDIUM: 1.0, ModelTier.LOW: 0.5}.get(tier, 5.0),
                                 api_model_id=entry["name"], required_clis=["gemini"]))
    return out

def opencode_models_metadata() -> list[ModelMetadata]:
    return [ModelMetadata(id=mid, family="opencode", tier=ModelTier.LOW, capabilities=[ModelCapability.CODING, ModelCapability.SPEED],
                          cost_factor=0.0, aliases=[mid.split("/", 1)[1]] if mid.startswith("opencode/") else [], required_clis=["opencode"])
            for mid in read_provider_cache().get("opencode") or []]

def ollama_models_metadata() -> list[ModelMetadata]:
    return [ModelMetadata(id=name, family="ollama", tier=ModelTier.HIGH, capabilities=[ModelCapability.CODING, ModelCapability.REASONING],
                          cost_factor=0.0, required_clis=["ollama"]) for name in read_provider_cache().get("ollama") or []]

def claude_config_files() -> list[Path]:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return [base / "stats-cache.json", Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json" if os.environ.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude.json"]

def claude_seen_models() -> list[str]:
    """Claude models this account's Claude Code has offered or used, from its own records (no key or login needed to read them)."""
    seen: list[str] = []
    stats, config = claude_config_files()
    try:
        seen += [str(m) for m in (json.loads(stats.read_text(encoding="utf-8")).get("modelUsage") or {})]
    except (OSError, ValueError, AttributeError):
        pass
    try:
        seen += [str(o.get("value") or "") for o in json.loads(config.read_text(encoding="utf-8")).get("additionalModelOptionsCache") or [] if isinstance(o, dict)]
    except (OSError, ValueError, AttributeError):
        pass
    cleaned = [re.sub(r"\[[^\]]*\]$", "", m).strip().lower() for m in seen]
    return sorted({m for m in cleaned if re.fullmatch(r"claude-[a-z0-9.-]+", m) and not m.endswith("-")})

def claude_models_metadata() -> list[ModelMetadata]:
    return [heuristic_classify(mid, "claude") for mid in claude_seen_models()]

def provider_login_metadata() -> list[ModelMetadata]:
    """Every model the providers say they can run, as registry entries."""
    return codex_login_metadata() + agy_models_metadata() + opencode_models_metadata() + ollama_models_metadata() + claude_models_metadata()

def model_usable(m: ModelMetadata) -> bool:
    """False for a model its provider says it can't run now, which would only fail and fall back. Unknown means usable."""
    if is_codex_model(m):
        return codex_usable(m)
    cache = read_provider_cache()
    if m.family == "gemini" and cache.get("agy") and preferred_cli("gemini") in {"agy", "antigravity"}:
        offered = {_norm(x) for entry in cache["agy"] for x in (entry["slug"], entry["name"])}
        return bool({_norm(m.id), _norm(m.api_model_id or "")} & offered)
    if m.family == "opencode" and cache.get("opencode"):
        return m.id in cache["opencode"] or f"opencode/{m.id}" in cache["opencode"]
    if m.family in {"ollama", "qwen"} and "ollama" in m.required_clis and cache.get("ollama"):
        installed = {name.split(":")[0] for name in cache["ollama"]} | set(cache["ollama"])
        wanted = {m.id, m.id.split(":")[0], "deepseek-coder" if m.id == "deepseek" else m.id}
        return bool(wanted & installed)
    return True

def model_substitute(m: ModelMetadata) -> ModelMetadata | None:
    """The model its provider does offer that best stands in for `m`: the closest standing, the person's own default or the highest effort."""
    if is_codex_model(m):
        return codex_substitute(m)
    pool = [c for c in provider_login_metadata() if set(c.required_clis) & set(m.required_clis) and c.id != m.id and model_usable(c)]
    if not pool:
        return None
    return min(pool, key=lambda c: (abs(int(c.tier) - int(m.tier)), 0 if c.id.endswith("-high") else 1 if c.id.endswith("-medium") else 2, c.id))

# --------------------------------------------------------------------------- when the list changes
# A computer-wide note of which models were available when the person last looked, so a change (a refresh, a new model in the
# Codex login's list, a new release of Orchestrator) can be pointed out once, at the top of the page.

_SOURCES_KEY: tuple | None = None

def _sources_key() -> tuple:
    """Changes whenever a file the registry is built from does."""
    paths = [Path(__file__).resolve().parents[1] / "config" / "models.json", Path.home() / ".orchestrator" / "custom_models.json",
             codex_home() / "auth.json", codex_home() / "models_cache.json", codex_home() / "config.toml",
             provider_cache_path(), *claude_config_files()]
    return tuple((p.stat().st_mtime_ns if p.exists() else 0) for p in paths)

def usable_model_ids() -> list[str]:
    """Every model that can be run now, rebuilt when a source file has changed (so a long-running server follows the list too)."""
    global _ALL_MODELS_CACHE, _SOURCES_KEY
    key = _sources_key()
    if key != _SOURCES_KEY:
        _SOURCES_KEY, _ALL_MODELS_CACHE = key, None
    return sorted(m.id for m in get_all_models() if model_usable(m))

def _seen_path() -> Path:
    return Path(os.environ.get("ORCHESTRATOR_USER_STATE_DIR") or Path.home() / ".orchestrator") / "models_seen.json"

def _names(ids: list[str], limit: int = 4) -> str:
    return ", ".join(ids[:limit]) + (f" and {len(ids) - limit} more" if len(ids) > limit else "")

def models_notice() -> dict[str, Any] | None:
    """What changed in the list of models since the person last dismissed the note, or None. The first time it is asked it
    only records the list."""
    import hashlib
    current = usable_model_ids()
    try:
        baseline = json.loads(_seen_path().read_text(encoding="utf-8")).get("baseline")
    except (OSError, ValueError, AttributeError):
        baseline = None
    if not isinstance(baseline, list):
        _write_baseline(current)
        return None
    added = [m for m in current if m not in baseline]
    removed = [m for m in baseline if m not in current]
    if not added and not removed:
        return None
    parts = ([f"New: {_names(added)}."] if added else []) + ([f"No longer available: {_names(removed)}."] if removed else [])
    return {"id": hashlib.sha1("\n".join(added + ["-"] + removed).encode()).hexdigest()[:10], "added": added, "removed": removed,
            "summary": " ".join(parts)}

def _write_baseline(ids: list[str]) -> None:
    path = _seen_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"baseline": ids}), encoding="utf-8")
    except OSError:
        pass

def dismiss_models_notice() -> None:
    """The person has seen the change: what is available now is the new starting point."""
    _write_baseline(usable_model_ids())

def get_model(model_id: str) -> ModelMetadata | None:
    # Check ID and aliases against all models
    all_m = get_all_models()
    for m in all_m:
        if model_id == m.id or model_id in m.aliases:
            return m
    return None

def get_free_models() -> list[ModelMetadata]:
    """Returns all available models with cost_factor == 0.0 (e.g. Ollama local models, OpenCode free models, GitHub Copilot)."""
    return [m for m in get_all_models() if m.cost_factor == 0.0]

def heuristic_classify(model_id: str, family: str) -> ModelMetadata:
    """Best-effort classification of a raw model ID into our metadata structure."""
    m_id = model_id.lower()
    tier = ModelTier.HIGH # Default
    caps = [ModelCapability.CODING, ModelCapability.REASONING]
    
    # Tier mapping
    if any(x in m_id for x in ["opus", "fable", "o1", "o3", "o4", "gpt-5", "gpt-6", "extreme"]):
        tier = ModelTier.LOW if "mini" in m_id else ModelTier.EXTREME
    elif any(x in m_id for x in ["sonnet", "pro", "gpt-4", "high"]):
        tier = ModelTier.LOW if "mini" in m_id else ModelTier.HIGH
    elif any(x in m_id for x in ["haiku", "flash", "gpt-3.5", "medium"]):
        tier = ModelTier.MEDIUM
    elif any(x in m_id for x in ["mini", "lite", "small", "low"]):
        tier = ModelTier.LOW

    aliases = []
    if family == "opencode" and model_id.startswith("opencode/"):
        aliases.append(model_id.split("opencode/", 1)[1])
        
    # Capability mapping
    if any(x in m_id for x in ["vision", "visual"]):
        caps.append(ModelCapability.VISION)
    if any(x in m_id for x in ["context", "128k", "1m", "2m"]):
        caps.append(ModelCapability.CONTEXT)
    if "flash" in m_id or "turbo" in m_id:
        caps.append(ModelCapability.SPEED)
        
    # Cost factor (rough guess)
    if family == "ollama" or "-free" in m_id or m_id.endswith("free"):
        cost = 0.0
    else:
        cost = 1.0
        if tier == ModelTier.EXTREME: cost = 10.0
        elif tier == ModelTier.HIGH: cost = 5.0
        elif tier == ModelTier.MEDIUM: cost = 1.0
        elif tier == ModelTier.LOW: cost = 0.5
    
    # CLI mapping
    cli = family
    if family == "openai": cli = "codex"
    
    return ModelMetadata(
        id=model_id,
        family=family,
        tier=tier,
        capabilities=list(set(caps)),
        cost_factor=cost,
        aliases=aliases,
        required_clis=[cli],
        api_model_id=model_id
    )

def parse_agy_models_with_names(output: str) -> list[tuple[str, str]]:
    """(slug, display name) for each model in `agy models` output, which lists "slug<TAB>Display Name (Effort)" per line."""
    found = []
    for line in output.splitlines():
        slug, _, name = line.strip().partition("\t")
        slug, name = slug.strip().lower(), name.strip()
        if slug and name and re.fullmatch(r"[a-z0-9][a-z0-9._-]*", slug):
            found.append((slug, name))
    return found

def parse_agy_models_output(output: str) -> list[str]:
    """Extracts model IDs from `agy models` text output."""
    models = []
    import re
    tabbed = parse_agy_models_with_names(output)
    if tabbed:
        return sorted({slug for slug, _name in tabbed})
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("available", "model ", "models")):
            continue

        line = line.lstrip("-*• \t")
        lower_line = line.lower()
        if not lower_line.startswith(("gemini", "claude", "gpt", "models/gemini", "models/claude", "models/gpt")):
            continue

        # If it has spaces, e.g., "Gemini 3.5 Flash (Medium)", slugify it
        if " " in line:
            slug = line.lower()
            slug = re.sub(r'[^a-z0-9\.]+', '-', slug)
            slug = re.sub(r'-+', '-', slug).strip('-')
            if slug:
                models.append(slug)
        else:
            token = line.split()[0].strip("`'\",")
            if token.startswith("models/"):
                token = token.split("/", 1)[1]
            if token:
                models.append(token.lower())

    return sorted(set(models))

def parse_opencode_models_output(output: str) -> list[str]:
    """Extracts model IDs from `opencode models` text output."""
    models = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith(("available", "models", "error", "usage")):
            continue
        line = line.lstrip("-*• \t")
        token = line.split()[0].strip("`'\",")
        if token:
            if not token.startswith("opencode/") and "/" not in token:
                token = f"opencode/{token}"
            models.append(token)
    return sorted(set(models))

def summarize_cli_error(output: str) -> str:
    """Returns the most actionable single line from CLI output."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return "unknown error"

    for line in lines:
        if line.lower().startswith("error:"):
            return line
    for line in lines:
        if "sign in" in line.lower() or "login" in line.lower():
            return line
    if "operation not permitted" in output.lower():
        return "CLI could not start (operation not permitted)"
    return lines[0]

def get_ssl_context() -> ssl.SSLContext:
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()

def urlopen_with_ssl_fallback(req_or_url, timeout: int = 5):
    import urllib.request
    import urllib.error
    import ssl
    ctx = get_ssl_context()
    try:
        return urllib.request.urlopen(req_or_url, timeout=timeout, context=ctx)
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "certificate verify failed" in str(e).lower():
            unverified_ctx = ssl._create_unverified_context()
            return urllib.request.urlopen(req_or_url, timeout=timeout, context=unverified_ctx)
        raise

def discover_from_sources_with_details(keys: dict[str, str] | None = None) -> DiscoveryResult:
    """Pings various provider APIs and CLIs to find new models.

    `keys` are API keys saved in settings, by environment variable name; a key in the environment wins.
    """
    import urllib.request
    import json
    discovered = []
    details = []
    keys = keys or {}

    def api_key(env_name: str) -> str:
        return os.environ.get(env_name) or str(keys.get(env_name) or "")

    # 0. Anthropic
    anthropic_key = api_key("ANTHROPIC_API_KEY")
    if anthropic_key:
        before = len(discovered)
        try:
            req = urllib.request.Request("https://api.anthropic.com/v1/models?limit=1000",
                                         headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01"})
            with urlopen_with_ssl_fallback(req, timeout=8) as resp:
                for m in json.loads(resp.read()).get("data", []):
                    if str(m.get("id", "")).startswith("claude"):
                        discovered.append(heuristic_classify(m["id"], "claude"))
            details.append(f"Anthropic API: found {len(discovered) - before} model(s).")
        except Exception as e:
            details.append(f"Anthropic API: failed ({e}).")
    else:
        details.append("Anthropic API: skipped (ANTHROPIC_API_KEY not set).")

    # 1. OpenAI
    openai_key = api_key("OPENAI_API_KEY")
    if openai_key:
        before = len(discovered)
        try:
            req = urllib.request.Request("https://api.openai.com/v1/models", 
                                         headers={"Authorization": f"Bearer {openai_key}"})
            with urlopen_with_ssl_fallback(req, timeout=5) as resp:
                data = json.loads(resp.read())
                exclude_prefixes = ("dall-e", "whisper", "tts", "text-embedding", "babbage", "davinci", "omni-moderation")
                for m in data.get("data", []):
                    mid = m["id"]
                    if (mid.startswith(("gpt-", "o1-", "o3-", "o4-", "chatgpt-", "codex-")) or any(x in mid.lower() for x in ["gpt", "o1", "o3", "o4"])) and not any(mid.startswith(x) for x in exclude_prefixes):
                        discovered.append(heuristic_classify(mid, "openai"))
            details.append(f"OpenAI API: found {len(discovered) - before} model(s).")
        except Exception as e:
            details.append(f"OpenAI API: failed ({e}).")
    else:
        details.append("OpenAI API: skipped (OPENAI_API_KEY not set).")

    # 1b. The Codex CLI's ChatGPT login: the models it lists for that plan (nothing to call, the CLI keeps the list fresh)
    offered = codex_login_models()
    if offered:
        discovered.extend(codex_login_metadata())
        details.append(f"Codex ChatGPT login: {len(offered)} model(s) ({', '.join(e['slug'] for e in offered)}).")
    elif (codex_home() / "auth.json").exists():
        details.append("Codex login: uses an API key or has no model list yet, so any OpenAI model name is tried.")

    # 1c. Claude Code's own record of the models it offered and this account has used
    seen = claude_models_metadata()
    if seen:
        discovered.extend(seen)
        details.append(f"Claude Code: {len(seen)} model(s) it offered or you have used ({', '.join(m.id for m in seen[:6])}).")

    # 2. Gemini
    gemini_key = api_key("GEMINI_API_KEY")
    if gemini_key:
        before = len(discovered)
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000&key={gemini_key}"  # the default page is only 50
            with urlopen_with_ssl_fallback(url, timeout=5) as resp:
                data = json.loads(resp.read())
                for m in data.get("models", []):
                    mid = m["name"].split("/")[-1]
                    if "gemini" in mid.lower():
                        discovered.append(heuristic_classify(mid, "gemini"))
            details.append(f"Gemini API: found {len(discovered) - before} model(s).")
        except Exception as e:
            details.append(f"Gemini API: failed ({e}).")
    else:
        details.append("Gemini API: skipped (GEMINI_API_KEY not set).")

    # 3. Antigravity CLI (OAuth/session-backed)
    agy_cli = preferred_cli("gemini")
    if agy_cli in {"agy", "antigravity"} and shutil.which(agy_cli):
        before = len(discovered)
        try:
            res = subprocess.run([agy_cli, "models"], capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                named = {slug: name for slug, name in parse_agy_models_with_names(res.stdout)}
                for mid in parse_agy_models_output(res.stdout):
                    found = heuristic_classify(mid, "gemini")
                    if mid in named:
                        found.api_model_id = named[mid]  # what `agy --model` is given
                    discovered.append(found)
                details.append(f"Antigravity CLI ({agy_cli}): found {len(discovered) - before} model(s).")
            else:
                msg = summarize_cli_error(f"{res.stderr}\n{res.stdout}")
                details.append(f"Antigravity CLI ({agy_cli}): unavailable ({msg}).")
        except Exception as e:
            details.append(f"Antigravity CLI ({agy_cli}): failed ({e}).")
    else:
        details.append("Antigravity CLI: skipped (agy not installed or not on PATH).")

    # 4. Ollama (Local)
    before = len(discovered)
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
            data = json.loads(resp.read())
            for m in data.get("models", []):
                mid = m["name"]
                discovered.append(heuristic_classify(mid, "ollama"))
        details.append(f"Ollama: found {len(discovered) - before} model(s).")
    except Exception as e:
        details.append(f"Ollama: unavailable ({e}).")

    # 4b. Ollama Cloud
    ollama_key = api_key("OLLAMA_API_KEY")
    if ollama_key:
        before = len(discovered)
        try:
            req = urllib.request.Request("https://ollama.com/api/tags", headers={"Authorization": f"Bearer {ollama_key}"})
            with urlopen_with_ssl_fallback(req, timeout=8) as resp:
                for m in json.loads(resp.read()).get("models", []):
                    discovered.append(heuristic_classify(m["name"], "ollama"))
            details.append(f"Ollama Cloud: found {len(discovered) - before} model(s).")
        except Exception as e:
            details.append(f"Ollama Cloud: failed ({e}).")

    # 5. OpenCode CLI
    if shutil.which("opencode"):
        before = len(discovered)
        try:
            res = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                for mid in parse_opencode_models_output(res.stdout):
                    discovered.append(heuristic_classify(mid, "opencode"))
                details.append(f"OpenCode CLI: found {len(discovered) - before} model(s).")
            else:
                msg = summarize_cli_error(f"{res.stderr}\n{res.stdout}")
                details.append(f"OpenCode CLI: unavailable ({msg}).")
        except Exception as e:
            details.append(f"OpenCode CLI: failed ({e}).")
    else:
        details.append("OpenCode CLI: skipped (opencode not installed or not on PATH).")

    return DiscoveryResult(models=discovered, details=details)

def models_last_synced() -> float | None:
    """When the model list was last refreshed on this computer, as a Unix time, or None if it never was."""
    path = Path.home() / ".orchestrator" / "custom_models.json"
    return path.stat().st_mtime if path.is_file() else None

def discover_from_sources() -> list[ModelMetadata]:
    """Pings various provider APIs to find new models."""
    return discover_from_sources_with_details().models

def sync_models(live_discovery: bool = False, keys: dict[str, str] | None = None) -> tuple[bool, str]:
    """
    Fetches latest model definitions and updates ~/.orchestrator/custom_models.json.
    - if live_discovery=True: Pings OpenAI/Gemini/Ollama APIs directly.
    - else: Fetches from the remote JSON registry, falling back to bundled models.
    """
    import urllib.request
    import urllib.error
    import ssl
    
    global_config = Path.home() / ".orchestrator" / "custom_models.json"
    global_config.parent.mkdir(parents=True, exist_ok=True)
    
    new_models = []
    source_name = "remote registry"

    if live_discovery:
        source_name = "live provider APIs"
        discovery = discover_from_sources_with_details(keys)
        new_models = discovery.models
        discovery_report = "\n".join(f"- {detail}" for detail in discovery.details)
        if not new_models:
            suffix = f"\n{discovery_report}" if discovery_report else ""
            return False, f"Live discovery found no new models.{suffix}"
    else:
        # Try to load registry URL from settings
        registry_url = os.environ.get(
            "MODEL_REGISTRY_URL",
            "https://raw.githubusercontent.com/leemosupreemo/orchestrator/main/orchestrator/config/models.json",
        )
        settings_path = Path.home() / ".orchestrator" / "config" / "settings.json"
        if "ORCHESTRATOR_PROJECT_ROOT" in os.environ:
            p_settings = Path(os.environ["ORCHESTRATOR_PROJECT_ROOT"]) / ".orchestrator" / "config" / "settings.json"
            if p_settings.exists(): settings_path = p_settings
            
        if settings_path.exists():
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                registry_url = settings.get("model_registry_url", registry_url)
            except: pass

        try:
            ctx = ssl._create_unverified_context()
            req = urllib.request.Request(registry_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=10, context=ctx) as response:
                remote_data = json.loads(response.read().decode("utf-8"))
                remote_models = remote_data.get("models", [])
                for rm in remote_models:
                    new_models.append(model_from_dict(rm))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                source_name = "bundled registry (remote not found)"
                new_models = load_bundled_models()
                if not new_models:
                    return False, f"Sync failed: Registry not found at {registry_url}, and no bundled registry was available."
            else:
                return False, f"Sync failed: HTTP Error {e.code}"
        except Exception as e:
            source_name = "bundled registry (remote unavailable)"
            new_models = load_bundled_models()
            if not new_models:
                return False, f"Sync failed: {e}"

    if not new_models:
        return False, f"No models found from {source_name}."
            
    # Load current global custom models to merge
    current_custom = {"models": []}
    if global_config.exists():
        try:
            current_custom = json.loads(global_config.read_text(encoding="utf-8"))
        except: pass
        
    # Merge logic
    merged_map = {m["id"]: m for m in current_custom.get("models", [])}
    for nm in new_models:
        # Convert ModelMetadata back to dict for JSON storage
        m_dict = {
            "id": nm.id,
            "family": nm.family,
            "tier": nm.tier.name,
            "capabilities": [c.value for c in nm.capabilities],
            "cost_factor": nm.cost_factor,
            "aliases": nm.aliases,
            "required_clis": nm.required_clis,
            "api_model_id": nm.api_model_id,
            "reasoning_effort": nm.reasoning_effort
        }
        m_dict = {k: v for k, v in m_dict.items() if v not in (None, [], "")}
        merged_map[nm.id] = m_dict
        
    new_data = {"models": sorted(list(merged_map.values()), key=lambda x: x["id"])}
    global_config.write_text(json.dumps(new_data, indent=2), encoding="utf-8")
    
    # Invalidate cache
    global _ALL_MODELS_CACHE
    _ALL_MODELS_CACHE = None
    
    if live_discovery:
        suffix = f"\n{discovery_report}" if discovery_report else ""
        return True, f"Successfully synced {len(new_models)} models from {source_name}.{suffix}"

    return True, f"Successfully synced {len(new_models)} models from {source_name}."
