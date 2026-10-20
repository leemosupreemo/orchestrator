"""The Orchestrator Editor plugin for Unity, and putting it into a Unity project.

The plugin is a Unity package (a folder with a package.json) shipped inside this one. Installing it copies the folder
into the project's `Packages/` folder, which makes it an "embedded" package: Unity picks it up with no registry or
git address, and it stays under the project's own version control.
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

PACKAGE_NAME = "com.leemosupreemo.orchestrator"
VERSION_FILE = Path("ProjectSettings") / "ProjectVersion.txt"
IGNORED = shutil.ignore_patterns("__pycache__", ".DS_Store", "*.pyc")
_GUID_NAMESPACE = uuid.UUID("5d0f6c0a-7c0e-4e0a-9d63-3a8f4f1c9b11")

_IMPORTERS = {
    ".cs": "MonoImporter:\n  externalObjects: {}\n  serializedVersion: 2\n  defaultReferences: []\n  executionOrder: 0\n"
           "  icon: {instanceID: 0}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
    ".asmdef": "AssemblyDefinitionImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n",
}
_TEXT_IMPORTER = "TextScriptImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n"
_FOLDER_IMPORTER = "folderAsset: yes\nDefaultImporter:\n  externalObjects: {}\n  userData: \n  assetBundleName: \n  assetBundleVariant: \n"


class PluginError(Exception):
    """A problem worth showing to the user as-is."""


def source_dir() -> Path:
    return Path(__file__).resolve().parent / "unity_plugin" / PACKAGE_NAME


def is_unity_project(root: Path) -> bool:
    return (root / VERSION_FILE).is_file() and (root / "Assets").is_dir()


def package_version(folder: Path) -> str | None:
    try:
        data = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(data.get("version")) if isinstance(data, dict) and data.get("version") else None


def installed_dir(root: Path) -> Path:
    return root / "Packages" / PACKAGE_NAME


def _files(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*"))
            if p.is_file() and p.name != ".DS_Store" and "__pycache__" not in p.parts}


def _referenced_in_manifest(root: Path) -> bool:
    try:
        manifest = json.loads((root / "Packages" / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    deps = manifest.get("dependencies") if isinstance(manifest, dict) else None
    return isinstance(deps, dict) and PACKAGE_NAME in deps


def install(root: Path, force: bool = False) -> dict[str, Any]:
    """Copy the plugin into `root/Packages`. Returns {"status": "installed"|"updated"|"current", "path", "version"}.
    An existing copy is only replaced with `force`, since it may carry someone's edits."""
    root = Path(root).expanduser().resolve()
    if not is_unity_project(root):
        raise PluginError(f"{root} is not a Unity project (no ProjectSettings/ProjectVersion.txt and Assets folder).")
    source, target = source_dir(), installed_dir(root)
    if not (source / "package.json").is_file():
        raise PluginError("This copy of Orchestrator doesn't include the Unity plugin. Update Orchestrator and try again.")
    if _referenced_in_manifest(root):
        raise PluginError(f"{PACKAGE_NAME} is already added in Packages/manifest.json. Update it there instead.")
    version = package_version(source)
    if target.exists():
        if _files(target) == _files(source):
            return {"status": "current", "path": str(target), "version": version}
        if not force:
            raise PluginError(f"{target} already exists (version {package_version(target) or 'unknown'}) and differs from this "
                              f"one ({version}). Run again with --force to replace it.")
        shutil.rmtree(target)
        status = "updated"
    else:
        status = "installed"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, ignore=IGNORED)
    return {"status": status, "path": str(target), "version": version}


# --------------------------------------------------------------------------- .meta files
# Unity ignores package files that have no .meta file, so each file and folder in the package ships with one. The GUID is
# derived from the path, so regenerating them never changes an existing GUID.


def meta_text(relative: str, is_folder: bool, suffix: str = "") -> str:
    guid = uuid.uuid5(_GUID_NAMESPACE, relative).hex
    importer = _FOLDER_IMPORTER if is_folder else _IMPORTERS.get(suffix, _TEXT_IMPORTER)
    return f"fileFormatVersion: 2\nguid: {guid}\n{importer}"


def write_meta_files(package: Path) -> list[Path]:
    """Create any missing .meta file in `package`; returns the ones written."""
    written = []
    for path in sorted(package.rglob("*")):
        if path.suffix == ".meta" or path.name == ".DS_Store" or "__pycache__" in path.parts:
            continue
        meta = path.with_name(path.name + ".meta")
        if not meta.exists():
            meta.write_text(meta_text(str(path.relative_to(package)), path.is_dir(), path.suffix), encoding="utf-8")
            written.append(meta)
    return written
