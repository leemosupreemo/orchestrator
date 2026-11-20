"""`orchestrator new`: the terminal version of starting a new project.

The prompts are passed in (ask / choose / confirm), so the flow is the same code
the tests drive. It shares its saved progress with the web UI: stop anywhere, and
either one picks up where you left off.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from orchestrator import new_project as np

GITHUB_CHOICE = "GitHub (recommended)"
LOCAL_CHOICE = "Local only (you can publish it later; jobs need GitHub)"


def run(*, ask: Callable[..., str], choose: Callable[[str, list[str]], str], choose_many: Callable[[str, list[str]], list[str]], confirm: Callable[[str, bool], bool],
        out: Callable[[str], None], github_state: Callable[[], dict[str, Any]], github_login: Callable[[], None]) -> tuple[int, Path | None]:
    """Returns (exit code, project root if one was created and the wizard should run on it)."""
    draft = np.load_draft()
    if draft and (draft.get("answers", {}).get("name") or draft.get("created_root")):
        label = draft["answers"].get("name") or draft.get("created_root")
        if confirm(f"You were setting up “{label}”. Pick up where you left off?", True):
            pass
        else:
            np.clear_draft()
            draft = None
    else:
        draft = None
    draft = draft or np.save_draft({"answers": {}, "step": "describe"})

    def save(**changes: Any) -> None:
        nonlocal draft
        draft = np.save_draft({**draft, **changes})

    def need_github() -> bool:
        """Block until GitHub is signed in. Returns False if the user chose local only; exits via _Stop if they pause."""
        while True:
            state = github_state()
            if state.get("user"):
                out(f"✓ Signed in to GitHub as {state['user']}")
                return True
            if not state.get("installed"):
                out("GitHub needs a one-time setup: install the GitHub CLI with  brew install gh")
                options = ["Check again", "Use local only for now", "Stop here (your answers are saved)"]
            else:
                out("You're not signed in to GitHub yet.")
                options = ["Sign in to GitHub now", "Check again", "Use local only for now", "Stop here (your answers are saved)"]
            pick = choose("What next?", options)
            if pick.startswith("Sign in"):
                github_login()
            elif pick.startswith("Use local"):
                return False
            elif pick.startswith("Stop"):
                save(waiting_on_github=True)
                raise _Stop

    try:
        # Already created locally, only GitHub left.
        if draft.get("created_root"):
            root = Path(draft["created_root"])
            out(f"Your project is already on this computer ({root}). Finishing GitHub.")
            if need_github():
                step = np.publish_to_github(root, draft["answers"].get("name") or root.name, draft.get("visibility", "private"))
                out(("✓ " if step["ok"] else "⚠ ") + f"{step['name']}: {step['detail']}")
                if not step["ok"]:
                    return 1, None
                np.clear_draft()
            else:
                np.clear_draft()
            return 0, root

        out("\nLet's describe what you're building. A few quick questions; short answers are fine.\n")
        answers = dict(draft["answers"])
        for q in np.QUESTIONS:
            if answers.get(q["key"]):
                continue
            if q["help"]:
                out(f"  ({q['help']})")
            if q["kind"] == "multi":
                picked: list[str] = []
                while not picked:
                    picked = choose_many(q["label"], q["options"])
                    if not picked:
                        out("  Pick at least one.")
                answers[q["key"]] = ", ".join(picked)
            elif q["kind"] == "choice":
                answers[q["key"]] = choose(q["label"], q["options"])
            elif q["kind"] == "area":
                answers[q["key"]] = _lines(ask, q["label"], q["required"])
            else:
                while True:
                    value = ask(q["label"], optional=not q["required"]).strip()
                    if value or not q["required"]:
                        break
                    out("  This one is needed.")
                answers[q["key"]] = value
            save(answers=answers, step="describe")
        save(step="where")

        host = "github" if choose("Where should it live?", [GITHUB_CHOICE, LOCAL_CHOICE]).startswith("GitHub") else "local"
        if host == "github" and not need_github():
            host = "local"
        visibility = draft.get("visibility", "private")
        if host == "github":
            visibility = "private" if choose("Who can see the repository?", ["Only me (private)", "Anyone (public)"]).startswith("Only") else "public"
        parent = ask("Which folder should it go in?", default=draft.get("parent")).strip() or draft["parent"]
        save(host=host, visibility=visibility, parent=parent, step="create")

        try:
            result = np.create_project(draft)
        except np.NewProjectError as exc:
            out(f"⚠ {exc}")
            return 1, None
        for s in result["steps"]:
            out(("✓ " if s["ok"] else "⚠ ") + f"{s['name']}" + (f": {s['detail']}" if s.get("detail") else ""))
        if result["github_ok"]:
            np.clear_draft()
        else:
            save(step="create", waiting_on_github=True, created_root=result["root"])
            out("\nYour project is saved locally. Run  orchestrator new  again to finish GitHub.")
        return 0, Path(result["root"])
    except _Stop:
        out("\nProgress saved. Run  orchestrator new  (or open Projects in the web UI) to continue where you left off.")
        return 0, None


class _Stop(Exception):
    pass


def _lines(ask: Callable[..., str], label: str, required: bool) -> str:
    """A multi-line answer: one item per prompt, a blank line finishes."""
    items: list[str] = []
    while True:
        prompt = f"{label} (one per line; blank line to finish)" if not items else "  next (blank line to finish)"
        value = ask(prompt, optional=bool(items) or not required).strip()
        if not value:
            if items or not required:
                return "\n".join(items)
            continue
        items.append(value)
