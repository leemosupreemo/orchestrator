"""Project-independent setup routes, guarded before project-specific handlers."""
from pathlib import Path

from orchestrator import account
from orchestrator.config_validation import validate_project_config
from orchestrator.project_config import load_project_config, load_recent_projects, remember_project
from orchestrator.project_setup import apply_project_setup, inspect_project


def project_ready(root: Path | None) -> bool:
    if root is None:
        return False
    try:
        return (root / ".orchestrator/project.json").is_file() and not validate_project_config(load_project_config(root))
    except (OSError, ValueError):
        return False


def dispatch_bootstrap(handler, method: str, parts: list[str]) -> bool:
    server = handler.server
    if server.bootstrap_enabled and not project_ready(server.root):
        if server.root:
            server.selected_root = server.root
        server.root = None
    setup_route = parts in (["bootstrap"], ["setup", "inspect"], ["setup", "apply"])
    if not setup_route and server.root is not None:
        return False
    handler._require_owner("set up this computer")
    if method == "GET" and parts in (["bootstrap"], ["state"]):
        info = {"needs_project": server.root is None,
                "selected_root": str(server.selected_root) if server.selected_root else None,
                "projects": load_recent_projects().get("projects", []),
                "runner": {"version": account.package_version(), "api_version": account.API_VERSION},
                "you": {"kind": "owner", "role": "owner"}}
        if parts == ["state"]:
            info.update(project=None, runs=[], actions={}, inbox=[], inbox_count=0, setup=info.copy(), token=server.token)
        handler._json(info)
    elif method == "POST" and parts in (["setup", "inspect"], ["setup", "apply"]):
        body = handler._body()
        raw = body.get("root")
        if not isinstance(raw, str) or not raw.strip():
            handler._json({"error": "Choose a project folder."}, 400)
            return True
        root = Path(raw).expanduser().resolve()
        try:
            if parts[-1] == "inspect":
                result = inspect_project(root)
                server.selected_root = root
            else:
                result = apply_project_setup(root, body.get("values") or {})
                if result["ok"]:
                    server.set_root(root)
                    handler._audit("project_setup", project=root.name)
            handler._json(result)
        except (OSError, ValueError) as exc:
            handler._json({"error": str(exc)}, 400)
    elif method == "GET" and parts == ["projects"]:
        handler._json({"projects": load_recent_projects().get("projects", []), "active": None})
    elif parts and parts[0] == "new-project":
        # These handlers already use per-user drafts and do not require a root.
        return False
    elif method == "POST" and parts == ["project"]:
        body = handler._body()
        root = Path(str(body.get("root") or "")).expanduser().resolve()
        if project_ready(root):
            server.set_root(root)
            remember_project(root, active=True)
            handler._json({"ok": True})
        else:
            handler._json({"error": "Set up this project first.", "code": "project_required"}, 409)
    else:
        handler._json({"error": "Choose or create a project to continue.", "code": "project_required"}, 409)
    return True
