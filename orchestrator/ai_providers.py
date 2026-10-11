"""How to get an AI working with Orchestrator: each provider, what it costs, and how to install and sign in.

This is the one list of providers: the setup checklist's tool and API-key lists and the API Keys page come from it.

Facts here change (prices, free tiers, install commands), so they live in one place and say when they were last
checked. The web page shows them with what's already installed and signed in on this computer, free options first.
"""
from __future__ import annotations

from typing import Callable

CHECKED = "October 2026"
COST_ORDER = {"free": 0, "free-limited": 1, "subscription": 2, "pay-per-use": 3}
COST_LABELS = {"free": "Free", "free-limited": "Free, with limits", "subscription": "Subscription or pay as you go",
               "pay-per-use": "Pay as you go"}

PROVIDERS: list[dict[str, str]] = [
    {"id": "opencode", "name": "OpenCode", "cli": "opencode", "cost": "free",
     "what": "Free built-in models, no account or card needed. The easiest way to try Orchestrator.",
     "install": "curl -fsSL https://opencode.ai/install | bash", "install_alt": "brew install opencode",
     "sign_in": "Nothing to sign in to for the free models. Turn on \"Free models only\" when you start a job.",
     "link": "https://opencode.ai/docs"},
    {"id": "ollama", "name": "Ollama", "cli": "ollama", "cost": "free",
     "what": "Runs open models on this computer: free and private, but it needs a well-equipped Mac (16 GB of memory or more for coding models).",
     "install": "brew install ollama", "install_alt": "", "install_note": "Or download the app from ollama.com/download.",
     "sign_in": "Download a model with `ollama pull <model>`; ollama.com/library lists them.",
     "link": "https://ollama.com/download", "key": "ollama_api_key", "env": "OLLAMA_API_KEY", "key_label": "Ollama Cloud"},
    {"id": "agy", "name": "Antigravity CLI (Google)", "cli": "agy", "cost": "free-limited",
     "what": "Sign in with a Google account. The free allowance is small (about 20 agent requests a day); paid Google plans raise it. It replaced Gemini CLI's free tier.",
     "install": "curl -fsSL https://antigravity.google/cli/install.sh | bash", "install_alt": "",
     "sign_in": "Run `agy` once and sign in with Google.", "link": "https://antigravity.google"},
    {"id": "claude", "name": "Claude Code (Anthropic)", "cli": "claude", "cost": "subscription",
     "what": "Included with a Claude Pro or Max plan, or pay as you go with an Anthropic API key.",
     "install": "curl -fsSL https://claude.ai/install.sh | bash", "install_alt": "",
     "sign_in": "Run `claude` once and sign in, or add an Anthropic API key under API Keys.",
     "link": "https://docs.claude.com/en/docs/claude-code/overview", "key": "anthropic_api_key", "env": "ANTHROPIC_API_KEY",
     "key_label": "Claude"},
    {"id": "codex", "name": "Codex (OpenAI)", "cli": "codex", "cost": "subscription",
     "what": "Included with ChatGPT Plus, Pro and Business plans, or pay as you go with an OpenAI API key.",
     "install": "curl -fsSL https://chatgpt.com/codex/install.sh | sh", "install_alt": "brew install --cask codex",
     "sign_in": "Run `codex` once and sign in with ChatGPT, or add an OpenAI API key under API Keys.",
     "link": "https://developers.openai.com/codex/cli", "key": "openai_api_key", "env": "OPENAI_API_KEY",
     "key_label": "Codex / OpenAI"},
    {"id": "gemini", "name": "Gemini CLI (Google)", "cli": "gemini", "cost": "pay-per-use",
     "what": "Needs a Gemini API key now that free Google-account access has ended. For free Google access, use the Antigravity CLI.",
     "install": "npm install -g @google/gemini-cli", "install_alt": "",
     "sign_in": "Add a Gemini API key under API Keys.", "link": "https://github.com/google-gemini/gemini-cli",
     "key": "gemini_api_key", "env": "GEMINI_API_KEY", "key_label": "Antigravity / Gemini"},
]

CLIS = [p["cli"] for p in PROVIDERS]
API_KEYS = [(p["key"], p["key_label"], p["env"]) for p in PROVIDERS if p.get("key")]  # (setting, label, environment variable)

# Optional helpers for the AI tools themselves (docs/recommended-mcp-plugins.md has the detail; a test keeps them in step).
PLUGINS: list[dict[str, str]] = [
    {"name": "GitHub MCP or GitHub plugin", "why": "Issues, pull requests, reviews and checks with full context."},
    {"name": "XcodeBuildMCP or build-ios-apps plugin", "why": "Build, run and screenshot iOS Simulator apps."},
    {"name": "Sentry MCP or Sentry plugin", "why": "Read crashes and errors while debugging."},
    {"name": "Playwright MCP", "why": "Drive and inspect web apps in a browser."},
]


def with_status(installed: Callable[[str], bool], ready: Callable[[str], bool], saved_keys: set[str]) -> dict:
    """The providers, free first, each with `installed` and `ready` (signed in, or an API key saved), and the plugins."""
    out = []
    for provider in sorted(PROVIDERS, key=lambda p: COST_ORDER[p["cost"]]):
        has_cli = installed(provider["cli"])
        key_saved = provider.get("key") in saved_keys
        out.append({**provider, "cost_label": COST_LABELS[provider["cost"]], "installed": has_cli,
                    "ready": (has_cli and ready(provider["cli"])) or key_saved, "key_saved": key_saved})
    return {"providers": out, "any_ready": any(p["ready"] for p in out), "plugins": PLUGINS, "checked": CHECKED}
