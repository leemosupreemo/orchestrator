from __future__ import annotations

from pathlib import Path

from orchestrator.project_config import ProjectConfig


def validate_project_config(config: ProjectConfig) -> list[str]:
    errors: list[str] = []

    if not config.project_name:
        errors.append("project_name is required.")
    if config.uses_xcode and not config.scheme:
        errors.append("scheme is required. Set it in .orchestrator/project.json.")
    if not config.xcode_project and not config.xcode_workspace and not config.build_command:
        errors.append("Configure xcode_project, xcode_workspace, or build_command.")
    if config.xcode_project and not (config.root / config.xcode_project).exists():
        errors.append(f"xcode_project does not exist: {config.xcode_project}")
    if config.xcode_workspace and not (config.root / config.xcode_workspace).exists():
        errors.append(f"xcode_workspace does not exist: {config.xcode_workspace}")
    if not config.test_command and (not config.uses_xcode or not config.test_target):
        errors.append("Configure test_target or test_command." if config.uses_xcode
                      else "Configure test_command for a non-Xcode project.")
    if config.firebase_distribution:
        if config.delivery_provider and config.delivery_provider != "firebase":
            errors.append("Only delivery_provider='firebase' is currently supported.")
        if not config.distribution_script_path:
            errors.append("firebase_distribution requires distribution_script_path. Run 'orchestrator wizard' to set this up.")
        elif not (config.root / config.distribution_script_path).exists():
            errors.append(f"distribution_script_path does not exist: {config.distribution_script_path}. Run 'orchestrator wizard' to generate it.")
        plist_path = config.firebase_plist_path
        if not plist_path:
            errors.append("firebase_distribution requires firebase_plist_path. Run 'orchestrator wizard' to set this up.")
        elif not (config.root / plist_path).exists():
            errors.append(f"firebase_plist_path does not exist: {plist_path}. Download this from Firebase Console.")
    if config.visual_app_path and not Path(config.visual_app_path).is_absolute() and not (config.root / config.visual_app_path).exists():
        errors.append(f"visual_app_path does not exist: {config.visual_app_path}")
    if config.visual_app_path and not config.app_bundle_id:
        errors.append("visual_app_path requires app_bundle_id for simulator launch.")
    if config.git_remote and ("github_pat_" in config.git_remote or (config.git_remote.startswith("http") and "@" in config.git_remote)):
        errors.append(
            "git_remote contains a hardcoded Personal Access Token (PAT). "
            "This leaks secrets and causes 403 write access failures. "
            "Update .orchestrator/project.json and run 'git remote set-url origin git@github.com:username/repository.git' to switch to SSH."
        )

    return errors


def validate_machine_config(config_dir: Path) -> list[str]:
    errors: list[str] = []
    machines_path = config_dir / "machines.json"
    if not machines_path.exists():
        return [f"Missing machines config: {machines_path}"]

    import json

    try:
        data = json.loads(machines_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"machines.json is invalid JSON: {exc}"]

    machines = data.get("machines")
    if not isinstance(machines, list) or not machines:
        return ["machines.json must contain at least one machine."]

    for index, machine in enumerate(machines):
        prefix = f"machines[{index}]"
        name = machine.get("name") or prefix
        mode = machine.get("execution_mode")
        if mode not in {"local", "ssh"}:
            errors.append(f"{name}: execution_mode must be 'local' or 'ssh'.")
        if not machine.get("repo_path"):
            errors.append(f"{name}: repo_path is required.")
        if mode == "ssh" and not machine.get("ssh_target"):
            errors.append(f"{name}: ssh_target is required for SSH machines.")
        if mode == "ssh" and not machine.get("orchestrator_package_path"):
            errors.append(
                f"{name}: orchestrator_package_path is recommended for SSH machines "
                "(default is ~/.orchestrator/package)."
            )

    return errors
