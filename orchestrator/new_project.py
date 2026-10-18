"""Starting a brand-new project (as opposed to adding one that already exists).

Flow: a few lean questions about the product -> where it lives (GitHub preferred,
or local only) -> create the folder, a product requirements doc the AI can read, a git repo,
and (optionally) the GitHub repo. Progress is saved as a draft so someone who has
to stop, say to sign in to GitHub, picks up where they left off.

Shared by the web UI and `orchestrator new`; no UI code here.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any

from orchestrator import prd
from orchestrator.project_config import remember_project, safe_resolve, user_state_dir


def _prd_question(section_id: str, key: str, required: bool = False) -> dict[str, Any]:
    """A question for one field of the product requirements doc, worded exactly as that field is (its title and hint),
    so the wizard and the Product page can't drift apart."""
    section = prd.BY_ID[section_id]
    return {"key": key, "label": section["title"], "help": section["hint"], "kind": "area", "required": required, "section": section_id}


# One question per field of the product requirements doc (see orchestrator/prd.py), in its order, after the two things
# that aren't part of it: the project's name and what it is built for. Like the doc's own fields, they are all optional.
QUESTIONS: list[dict[str, Any]] = [
    {"key": "name", "label": "What's it called?", "help": "A working name is fine. It becomes the folder name.", "kind": "text", "required": True, "section": "project"},
    {"key": "platform", "label": "What are you building it for?", "help": "Pick every platform you want, or let the AI recommend.", "kind": "multi", "required": True,
     "options": ["iOS app", "Android app", "macOS app", "Windows or Linux app", "Web app", "Backend / API", "Command-line tool or library", "Not sure: recommend for me"], "section": "project"},
    _prd_question("pitch", "pitch"),
    _prd_question("who", "audience"),
    _prd_question("features", "features"),
    _prd_question("look", "look"),
    _prd_question("not", "not"),
]
PRD_SECTIONS: list[dict[str, Any]] = [
    {"id": s["id"], "title": s["title"], "keys": [q["key"] for q in QUESTIONS if q["section"] == s["id"]], "description": s["hint"]}
    for s in prd.SECTIONS
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


def _fold_legacy(answers: dict[str, Any]) -> dict[str, Any]:
    """Earlier drafts asked "what itch" and "technology" as questions of their own; both belong to the Pitch now."""
    folded = {k: v for k, v in answers.items() if k not in ("problem", "stack")}
    extra = [f"{label}: {str(answers[key]).strip()}" for key, label in (("problem", "Problem"), ("stack", "Technology preferences")) if str(answers.get(key) or "").strip()]
    if extra:
        folded["pitch"] = "\n\n".join([str(folded.get("pitch") or "").strip(), *extra]).strip()
    return folded


def load_draft() -> dict[str, Any] | None:
    try:
        data = json.loads(draft_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("answers"), dict):
        data["answers"] = _fold_legacy(data["answers"])
    return data


def designs_dir() -> Path:
    return user_state_dir() / "new-project-designs"


def _kept_designs(value: Any) -> list[dict[str, str]]:
    """Only uploads that are really in the designs folder, so a draft can't point anywhere else."""
    kept: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        file = Path(str((item or {}).get("file") or "")).name
        if file and (designs_dir() / file).is_file():
            kept.append({"name": str(item.get("name") or file)[:120], "file": file})
    return kept[:20]


def add_design(name: str, data: bytes) -> dict[str, Any]:
    """Hold a design reference with the draft until the project exists to keep it in."""
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name or "").name).strip(".-")[:80]
    if not clean:
        raise NewProjectError("Give the file a name.")
    if not data:
        raise NewProjectError("That file is empty.")
    prior = load_draft() or {}
    designs = _kept_designs(prior.get("designs"))
    if len(designs) >= 20:
        raise NewProjectError("That's plenty of references. Remove one to add another.")
    designs_dir().mkdir(parents=True, exist_ok=True)
    file = f"{secrets.token_hex(3)}-{clean}"
    (designs_dir() / file).write_bytes(data)
    return save_draft({**prior, "designs": designs + [{"name": clean, "file": file}]})


def save_draft(raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only known fields, so a draft can't smuggle anything else in."""
    prior = load_draft() or {}
    given = _fold_legacy(raw.get("answers") or {})
    answers = {q["key"]: str(given.get(q["key"]) or "").strip()[:2000] for q in QUESTIONS}
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
        "designs": _kept_designs(raw["designs"] if "designs" in raw else prior.get("designs")),
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
    shutil.rmtree(designs_dir(), ignore_errors=True)


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


def render_prd(a: dict[str, str]) -> str:
    """The product requirements doc (see orchestrator/prd.py) filled in from the wizard's answers; anything unanswered keeps its guidance."""
    text = prd.template(a["name"])
    chosen = platform_list(a.get("platform", ""))
    recommend = RECOMMEND in chosen
    chosen = [p for p in chosen if p != RECOMMEND]
    pitch = [a.get("pitch", "").strip()]
    if a.get("problem", "").strip():
        pitch.append(f"Problem: {a['problem'].strip()}")
    if chosen:
        pitch.append("Built for: " + ", ".join(chosen))
    elif recommend:
        pitch.append("Built for: not decided yet. The AI should recommend platforms, with reasons, in its first plan.")
    if a.get("stack", "").strip():
        pitch.append(f"Technology preferences: {a['stack'].strip()}")
    pitch = [part for part in pitch if part]
    if pitch:
        text = prd.replace_section(text, "pitch", "\n\n".join(pitch))
    if a.get("audience", "").strip():
        text = prd.replace_section(text, "who", a["audience"].strip())
    features = feature_lines(a.get("features", ""))
    if features:
        body = "\n".join(f"- {f}" for f in features)
        if a.get("done", "").strip():
            body += f"\n\nVersion 1 is done when: {a['done'].strip()}"
        text = prd.replace_section(text, "features", body)
    if a.get("look", "").strip():
        text = prd.replace_section(text, "look", a["look"].strip())
    if a.get("not", "").strip():
        text = prd.replace_section(text, "not", a["not"].strip())
    return text


def render_agents(name: str) -> str:
    return (f"# {name}: instructions for AI helpers\n\n"
            f"Read `{prd.PATH}` first. It says what this product is, who it's for, what it must do, how it should look and feel, and what it must not be. "
            "It is a living document: it is kept up to date as jobs finish, and the person can edit it any time.\n\n"
            "Rules:\n- Prefer minimal, reviewable changes.\n- Build one working end-to-end slice at a time; keep the build and tests passing.\n"
            "- Don't build anything listed under \"Not this\".\n- If the document and the code disagree, ask before deciding which is right.\n\n")


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


def configure(root: Path, name: str) -> None:
    """Write .orchestrator/project.json and its config, exactly as `orchestrator init` does, with detected defaults."""
    import argparse
    import contextlib
    import io
    from orchestrator.cli import init_project
    with contextlib.redirect_stdout(io.StringIO()):
        init_project(argparse.Namespace(root=None, project=str(root), project_name=name, scheme=None, test_target=None,
                                        build_command=None, test_command=None, base_branch="main", force=False,
                                        with_starter_docs=False, with_helper_script=False))


def create_local(answers: dict[str, str], parent: str, designs: list[dict[str, str]] | None = None) -> tuple[Path, list[dict[str, Any]]]:
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
    (root / prd.PATH).parent.mkdir(parents=True, exist_ok=True)
    prd_text = render_prd(answers)
    kept = _kept_designs(designs)
    if kept:
        (root / prd.DESIGNS_DIR).mkdir(parents=True, exist_ok=True)
        links = []
        for item in kept:
            shutil.copyfile(designs_dir() / item["file"], root / prd.DESIGNS_DIR / item["file"])
            label = re.sub(r"[\[\]\n]", " ", item["name"]).strip()
            links.append(f"- [{label}](designs/{item['file']})")
        look = prd.split(prd_text).get("look", "")
        prd_text = prd.replace_section(prd_text, "look", (look + "\n" if look else "") + "\n".join(links))
    (root / prd.PATH).write_text(prd_text, encoding="utf-8")
    (root / "AGENTS.md").write_text(render_agents(answers["name"]), encoding="utf-8")
    summary = answers.get("pitch", "").strip()
    (root / "README.md").write_text(f"# {answers['name']}\n\n{summary + chr(10) + chr(10) if summary else ''}See [the product requirements](docs/product/prd.md).\n", encoding="utf-8")
    steps.append({"name": "Created the folder and product requirements", "ok": True, "detail": str(root)})

    # The same setup `orchestrator init` does, so jobs can run without a separate "configure project" step.
    try:
        configure(root, answers["name"])
        steps.append({"name": "Configured the project for Orchestrator", "ok": True, "detail": ".orchestrator/project.json"})
    except Exception as exc:  # the project itself exists; the checkup can still configure it
        steps.append({"name": "Configured the project for Orchestrator", "ok": False, "detail": f"Not done yet ({exc}). Finish it from Set up this project."})

    init = _git(["init", "-q", "-b", "main"], root)
    if init.returncode != 0:
        steps.append({"name": "Set up git", "ok": False, "detail": (init.stderr or init.stdout).strip()})
        return root, steps
    _git(["add", "."], root)
    commit = _git(["commit", "-q", "-m", "Start project with product requirements"], root)
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
    root, steps = create_local(answers, draft.get("parent") or default_parent(), draft.get("designs"))
    github = draft.get("host") == "github"
    if github:
        steps.append(publish_to_github(root, answers["name"], draft.get("visibility", "private")))
    remember_project(root, answers["name"], active=True)
    steps.append({"name": "Added to your projects", "ok": True, "detail": answers["name"]})
    return {"root": str(root), "name": answers["name"], "steps": steps, "github": github,
            "platform_needs": platform_needs(answers.get("platform", "")), "recommend_platform": RECOMMEND in platform_list(answers.get("platform", "")),
            "github_ok": (not github) or steps[-2]["ok"],
            "prd_filled": any(str(answers.get(key) or "").strip() for key in ("pitch", "audience", "features", "look", "not"))}
