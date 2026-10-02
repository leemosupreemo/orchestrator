"""Starting a brand-new project (as opposed to adding one that already exists).

Flow: a few lean questions about the product -> where it lives (GitHub preferred,
or local only) -> create the folder, a product brief the AI can read, a git repo,
and (optionally) the GitHub repo. Progress is saved as a draft so someone who has
to stop, say to sign in to GitHub, picks up where they left off.

Shared by the web UI and `orchestrator new`; no UI code here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import product_docs
from orchestrator.project_config import remember_project, safe_resolve, user_state_dir

# (key, label, help, kind, required, options)
QUESTIONS: list[dict[str, Any]] = [
    {"key": "name", "label": "What's it called?", "help": "A working name is fine. It becomes the folder name.", "kind": "text", "required": True},
    {"key": "pitch", "label": "What is it, in one sentence?", "help": "e.g. A turn-based word game you can play with friends.", "kind": "text", "required": True},
    {"key": "audience", "label": "Who is it for?", "help": "The people who'll use it.", "kind": "text", "required": True},
    {"key": "problem", "label": "What problem does it solve for them?", "help": "Why would they want it?", "kind": "area", "required": True},
    {"key": "features", "label": "What must it do on day one?", "help": "Up to five things, one per line. Keep it to the essentials.", "kind": "area", "required": True},
    {"key": "platform", "label": "What are you building it for?", "help": "Pick every platform you want, or let the AI recommend.", "kind": "multi", "required": True,
     "options": ["iOS app", "Android app", "macOS app", "Windows or Linux app", "Web app", "Backend / API", "Command-line tool or library", "Not sure: recommend for me"]},
    {"key": "stack", "label": "Any technology you want, or want to avoid?", "help": "Optional. e.g. SwiftUI, no third-party UI kits.", "kind": "area", "required": False},
    {"key": "done", "label": "How will you know version 1 works?", "help": "Optional. The test you'd run to say \"yes, that's it\".", "kind": "area", "required": False},
]
REQUIRED = [q["key"] for q in QUESTIONS if q["required"]]
PLATFORMS = next(q["options"] for q in QUESTIONS if q["key"] == "platform")
RECOMMEND = "Not sure: recommend for me"

# What each choice needs before jobs can build, test and ship it.
PLATFORM_NEEDS: dict[str, list[str]] = {
    "iOS app": ["An Xcode project with a scheme and a test target", "Firebase App Distribution (or TestFlight) to put builds on testers' phones", "Simulator checks for screens"],
    "Android app": ["A Gradle project with unit and instrumented test tasks", "Firebase App Distribution to put builds on testers' phones"],
    "macOS app": ["An Xcode project with a scheme and a test target", "Code signing for sharing builds"],
    "Windows or Linux app": ["A build toolchain for the framework you pick", "A packaging step testers can install"],
    "Web app": ["A package manager and a dev server", "A unit test runner plus a browser test tool", "Hosting for a preview link"],
    "Backend / API": ["A runtime and a test runner", "A way to run it locally with a database", "A deploy target"],
    "Command-line tool or library": ["A language toolchain and test runner", "A package registry if others will install it"],
}
STEPS = ["describe", "where", "create", "done"]


class NewProjectError(Exception):
    """A problem worth showing to the user as-is."""


# --------------------------------------------------------------------------- draft (resume where you left off)


def draft_path() -> Path:
    return user_state_dir() / "new-project-draft.json"


def load_draft() -> dict[str, Any] | None:
    try:
        data = json.loads(draft_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_draft(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only known fields, so a draft can't smuggle anything else in."""
    prior = load_draft() or {}
    answers = {q["key"]: str(((raw.get("answers") or {}).get(q["key"])) or "").strip()[:2000] for q in QUESTIONS}
    step = raw.get("step") if raw.get("step") in STEPS[:3] else prior.get("step", "describe")
    choice = raw.get("host") if raw.get("host") in ("github", "local") else prior.get("host")
    visibility = raw.get("visibility") if raw.get("visibility") in ("private", "public") else prior.get("visibility", "private")
    draft = {
        "answers": answers, "step": step, "host": choice, "visibility": visibility,
        "parent": str(raw.get("parent") or prior.get("parent") or default_parent())[:500],
        # Chose GitHub but isn't signed in yet: where to pick up again.
        "waiting_on_github": bool(raw.get("waiting_on_github", False)),
        # Set once the local project exists, so finishing GitHub later publishes it instead of recreating it.
        "created_root": str(raw.get("created_root") or prior.get("created_root") or "")[:500],
    }
    path = draft_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(draft, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return draft


def clear_draft() -> None:
    try:
        draft_path().unlink()
    except OSError:
        pass


def default_parent() -> str:
    return str(Path.home() / "Projects")


def platform_list(value: str) -> list[str]:
    """The platforms in an answer, in the order given. Accepts the old single-choice values too."""
    out = []
    for item in re.split(r"[,\n]", value or ""):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def platform_needs(value: str) -> list[dict[str, Any]]:
    """What to set up next for each chosen platform (none for "recommend" or free text)."""
    return [{"platform": p, "needs": PLATFORM_NEEDS[p]} for p in platform_list(value) if p in PLATFORM_NEEDS]


def missing_answers(answers: dict[str, str]) -> list[str]:
    return [q["label"] for q in QUESTIONS if q["required"] and not str(answers.get(q["key"], "")).strip()]


# --------------------------------------------------------------------------- the brief


def slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name.strip()).strip("-")
    return slug[:60]


def feature_lines(text: str) -> list[str]:
    items = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() for line in text.splitlines()]
    return [i for i in items if i][:8]


def render_brief(a: dict[str, str]) -> str:
    """The product brief: plain markdown any AI (or person) can read first."""
    features = feature_lines(a.get("features", ""))
    lines = [f"# {a['name']}: product brief", "",
             "_Written when the project was started. The orchestrator and any AI helping on this project should read this first, "
             "and keep it up to date as the product changes._", "",
             "## What it is", "", a["pitch"], "",
             "## Who it's for", "", a["audience"], "",
             "## The problem", "", a["problem"], "",
             "## Version 1 must do", ""]
    lines += [f"{i}. {f}" for i, f in enumerate(features, 1)] or ["_Not specified yet._"]
    chosen = platform_list(a.get("platform", ""))
    recommend = RECOMMEND in chosen
    chosen = [p for p in chosen if p != RECOMMEND]
    lines += ["", "## Platforms", ""]
    if recommend:
        lines += ["_Not decided._ The AI should recommend platforms, with reasons, in its first plan, based on the audience and problem above.", ""]
    if chosen:
        lines += [f"- {p}" for p in chosen]
    elif not recommend:
        lines.append("Not specified")
    lines.append("")
    if a.get("stack"):
        lines += ["## Technology preferences", "", a["stack"], ""]
    if a.get("done"):
        lines += ["## How we'll know version 1 works", "", a["done"], ""]
    lines += ["## Out of scope for version 1", "", "_Add things that are explicitly not being built yet, so nobody builds them by accident._", ""]
    return "\n".join(lines)


def render_agents(name: str) -> str:
    return (f"# {name}: instructions for AI helpers\n\n"
            "Read `docs/product-brief.md` first. It says what this product is, who it's for and what version 1 must do. Then read, in order:\n\n"
            "- `docs/product/use-cases.md`: the users, the core use cases, and the non-goals (do not build non-goals)\n"
            "- `docs/product/journey.md`: the path a person takes from first contact to repeat use\n"
            "- `docs/product/screens.md`: every screen and why it exists\n"
            "- `docs/product/architecture-decisions.md`: what is hard to change, and what was decided\n"
            "- `docs/product/plan.md`: the slice being built now\n\n"
            "Rules:\n- Prefer minimal, reviewable changes.\n- Build one working end-to-end slice at a time; keep the build and tests passing.\n"
            "- Don't build anything the documents list as a non-goal.\n- If the documents and the code disagree, ask before deciding which is right.\n")


# --------------------------------------------------------------------------- creating it


def _git(args: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _within_home(path: Path) -> bool:
    home = safe_resolve(Path.home())
    return path == home or home in path.parents


def target_dir(parent: str, name: str) -> Path:
    slug = slugify(name)
    if not slug:
        raise NewProjectError("Give the project a name using letters or numbers.")
    base = safe_resolve(Path(parent).expanduser())
    if not _within_home(base):
        raise NewProjectError("Choose a folder inside your home folder.")
    return base / slug


def create_local(answers: dict[str, str], parent: str) -> tuple[Path, list[dict[str, Any]]]:
    """Folder + brief + git repo. Returns (root, steps). Raises NewProjectError if it can't start."""
    missing = missing_answers(answers)
    if missing:
        raise NewProjectError("Still needed: " + "; ".join(missing))
    root = target_dir(parent, answers["name"])
    if root.exists() and any(root.iterdir()):
        raise NewProjectError(f"{root} already exists and isn't empty. Pick another name or folder.")
    steps: list[dict[str, Any]] = []
    try:
        (root / "docs").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NewProjectError(f"Couldn't create {root}: {exc.strerror}") from None
    (root / "docs" / "product-brief.md").write_text(render_brief(answers), encoding="utf-8")
    product_docs.scaffold(root)
    (root / "AGENTS.md").write_text(render_agents(answers["name"]), encoding="utf-8")
    (root / "README.md").write_text(f"# {answers['name']}\n\n{answers['pitch']}\n\nSee [the product brief](docs/product-brief.md).\n", encoding="utf-8")
    steps.append({"name": "Created the folder and product brief", "ok": True, "detail": str(root)})

    init = _git(["init", "-q", "-b", "main"], root)
    if init.returncode != 0:
        steps.append({"name": "Set up git", "ok": False, "detail": (init.stderr or init.stdout).strip()})
        return root, steps
    _git(["add", "."], root)
    commit = _git(["commit", "-q", "-m", "Start project with product brief"], root)
    steps.append({"name": "Set up git", "ok": commit.returncode == 0,
                  "detail": "First commit made." if commit.returncode == 0 else
                  ((commit.stderr or commit.stdout).strip() or "Couldn't commit. Set git user.name and user.email, then commit.")})
    return root, steps


def publish_to_github(root: Path, name: str, visibility: str) -> dict[str, Any]:
    """Create the GitHub repo from the local one and push. Never raises: returns a step."""
    from orchestrator.setup_checklist import github_cli_state
    state = github_cli_state(fresh=True)
    if not state["installed"]:
        return {"name": "Create the GitHub repository", "ok": False, "detail": "The GitHub CLI isn't installed (brew install gh)."}
    if not state["user"]:
        return {"name": "Create the GitHub repository", "ok": False, "detail": "Not signed in to GitHub yet."}
    flag = "--public" if visibility == "public" else "--private"
    try:
        res = subprocess.run(["gh", "repo", "create", slugify(name), flag, "--source", ".", "--remote", "origin", "--push"],
                             cwd=root, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return {"name": "Create the GitHub repository", "ok": False, "detail": "GitHub took too long to answer."}
    if res.returncode != 0:
        return {"name": "Create the GitHub repository", "ok": False, "detail": (res.stderr or res.stdout).strip()[-400:]}
    url = (res.stdout.strip().splitlines() or [""])[-1]
    return {"name": "Create the GitHub repository", "ok": True, "detail": url or f"{state['user']}/{slugify(name)}", "url": url}


def create_project(draft: dict[str, Any]) -> dict[str, Any]:
    """Run the whole creation from a saved draft. Local creation is all-or-nothing up front;
    GitHub is attempted after, and a GitHub failure keeps the local project and says what's left."""
    answers = draft.get("answers") or {}
    root, steps = create_local(answers, draft.get("parent") or default_parent())
    github = draft.get("host") == "github"
    if github:
        steps.append(publish_to_github(root, answers["name"], draft.get("visibility", "private")))
    remember_project(root, answers["name"], active=True)
    steps.append({"name": "Added to your projects", "ok": True, "detail": answers["name"]})
    return {"root": str(root), "name": answers["name"], "steps": steps, "github": github,
            "platform_needs": platform_needs(answers.get("platform", "")), "recommend_platform": RECOMMEND in platform_list(answers.get("platform", "")),
            "github_ok": (not github) or steps[-2]["ok"]}
