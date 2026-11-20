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
        wanted = str(body.get("root") or "").strip()
        if not wanted:
            handler._json({"error": "Project root path is required"}, 400)
            return True
        root = Path(wanted).expanduser().resolve()
        if not root.is_dir():
            fallback = Path.home() / "Projects" / Path(wanted).name
            if fallback.is_dir():
                root = fallback.resolve()
        from orchestrator.web.server import is_path_accessible
        if not is_path_accessible(root):
            fallback = Path.home() / "Projects" / root.name
            if fallback != root and fallback.is_dir() and is_path_accessible(fallback):
                root = fallback.resolve()
            else:
                handler._json({"error": f"Cannot access project directory '{root}'. Check folder permissions or iCloud Drive status."}, 400)
                return True
        if project_ready(root):
            server.set_root(root)
            from orchestrator.web.server import detect_project_source, project_state, runtime_dir, _safe_read_json_file
            p_cfg = _safe_read_json_file(runtime_dir(root) / "project.json")
            s_type, s_label, g_repo = detect_project_source(root, p_cfg)
            remember_project(root, active=True, source_type=s_type, source_label=s_label, github_repo=g_repo)
            handler._json({"ok": True, "project": project_state(root)})
        else:
            handler._json({"error": "Set up this project first.", "code": "project_required"}, 409)
    else:
        handler._json({"error": "Choose or create a project to continue.", "code": "project_required"}, 409)
    return True
