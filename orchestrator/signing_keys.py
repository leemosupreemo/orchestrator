"""Signing secrets, per platform: where they are, and how to get them onto the computer that builds.

A project's settings are shared (committed), but the secrets that sign its builds live on one computer. A new computer
fails late, in the middle of a build, with a message about a folder. This module answers earlier, in plain terms:

    iOS      the App Store Connect API key (AuthKey_<id>.p8) xcodebuild reads from ~/.private_keys
    Android  the release keystore, and the settings file (key.properties) that names it, when the project signs releases

For each it says whether the secret is here, not set up yet, or on another computer, and it can:

    import  a key downloaded from Apple into the right folder (iOS), keeping the original where it was
    copy    a secret between this computer and one of your SSH computers, over SSH only

Rules for everything here:
  - Secret contents are never returned, printed or logged. Only names, sizes and "same / different" come back.
  - The browser never supplies a path. A secret is named by the project (its Gradle files, its key id), and the
    destination is derived from it: the home folder, or a git-ignored path inside the project.
  - A different file already at the destination is never replaced unless asked. A link is never followed or written through.
  - Copies are owner-only (600) and checked afterwards by hash.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from orchestrator import project_config
from orchestrator.project_config import (DEFAULT_RUNTIME_DIRNAME, asc_key_is_on_another_computer, locate_asc_key, safe_resolve,
                                         secure_key_dirs)

KEY_FILE = re.compile(r"AuthKey_([A-Za-z0-9]{6,20})\.p8")
KEY_ID = re.compile(r"[A-Za-z0-9]{6,20}")
ISSUER_ID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
PEM_HEADER = b"-----BEGIN PRIVATE KEY-----"
IOS_KEY_MAX = 4096
ANDROID_FILE_MAX = 262_144
APP_STORE_CONNECT_KEYS_URL = "https://appstoreconnect.apple.com/access/integrations/api"
DOWNLOAD_FOLDERS = ("Downloads", "Desktop")
SKIP_DIRS = {".git", "node_modules", "build", ".gradle", "Pods", "Library", "Temp", "DerivedData", ".dart_tool", ".idea", "Carthage"}
SSH_TIMEOUT = 30

Run = Callable[[list[str], "bytes | None", int], "tuple[int, bytes, bytes]"]


class SecretError(Exception):
    """Something the person can act on. The text never contains secret contents."""


def run_process(argv: list[str], data: bytes | None, timeout: int) -> tuple[int, bytes, bytes]:
    try:
        done = subprocess.run(argv, input=data, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, b"", b"timed out"
    except OSError as exc:
        return 127, b"", str(exc).encode()
    return done.returncode, done.stdout, done.stderr


def platform_of(config: dict[str, Any]) -> str:
    return str(config.get("delivery_platform") or "ios").strip().lower()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _looks_like_apple_key(data: bytes) -> bool:
    return data.lstrip().startswith(PEM_HEADER) and len(data) <= IOS_KEY_MAX


def _home() -> Path:
    return safe_resolve(Path.home())


def runtime_dir(root: Path) -> Path:
    return root / DEFAULT_RUNTIME_DIRNAME


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def update_project_json(root: Path, changes: dict[str, Any]) -> None:
    path = runtime_dir(root) / "project.json"
    project = _read_json(path)
    for key, value in changes.items():
        if value is None:
            project.pop(key, None)
        else:
            project[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------------------------------------------------- iOS

def downloaded_keys() -> list[dict[str, Any]]:
    """AuthKey_<id>.p8 files waiting in Downloads or on the Desktop of this computer: names only, newest first."""
    found: list[tuple[float, dict[str, Any]]] = []
    home = _home()
    for folder in DOWNLOAD_FOLDERS:
        try:
            entries = list((home / folder).iterdir())[:2000]
        except OSError:
            continue
        for path in entries:
            match = KEY_FILE.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                if stat.st_size > IOS_KEY_MAX or not _looks_like_apple_key(path.read_bytes()):
                    continue
            except OSError:
                continue
            found.append((stat.st_mtime, {"name": path.name, "folder": folder, "id": match.group(1), "modified": stat.st_mtime}))
    return [item for _, item in sorted(found, key=lambda pair: -pair[0])][:6]


def issuer_hints(projects: list[tuple[str, Path]], skip: Path) -> list[dict[str, str]]:
    """Issuer IDs already used by the user's other projects on this computer. A team's keys share one issuer."""
    seen: dict[str, dict[str, str]] = {}
    for name, root in projects:
        if safe_resolve(root) == safe_resolve(skip):
            continue
        issuer = str(_read_json(runtime_dir(root) / "project.json").get("asc_issuer_id") or "").strip()
        if ISSUER_ID.fullmatch(issuer) and issuer not in seen:
            seen[issuer] = {"from": name, "issuer": issuer}
    return list(seen.values())[:4]


def ios_view(config: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Where the iOS signing key stands on this computer. None when the project doesn't deliver an iOS build itself."""
    if platform_of(config) != "ios":
        return None
    key_id = str(config.get("asc_key_id") or "").strip()
    issuer = str(config.get("asc_issuer_id") or "").strip()
    key_path = str(config.get("asc_key_path") or "").strip()
    valid_id = bool(KEY_ID.fullmatch(key_id))
    name = f"AuthKey_{key_id}.p8" if valid_id else (Path(key_path).name if key_path else "")
    view: dict[str, Any] = {"platform": "ios", "label": "iOS signing key", "key_id": key_id if valid_id else "", "file": name,
                            "folder": "~/.private_keys", "keys_url": APP_STORE_CONNECT_KEYS_URL,
                            "signing_style": str(config.get("signing_style") or "automatic")}
    if not (key_id or key_path or issuer):
        view["state"] = "none"
        return view
    found = locate_asc_key(key_path or f"~/.private_keys/{name}", key_id, root) if (key_path or valid_id) else None
    here = bool(found and found.parent in secure_key_dirs() and found.is_file())
    if not (valid_id and issuer and (key_path or here)):
        view["state"] = "partial"
        return view
    if not here:
        view["state"] = "missing"
        view["on_another_computer"] = asc_key_is_on_another_computer(key_path)
        return view
    mode = found.stat().st_mode
    view["state"] = "ready" if (mode & 0o077) == 0 else "too-open"
    return view


def import_ios_key(root: Path, filename: str, issuer: str, *, replace: bool = False, say: Callable[[str], None] = print) -> dict[str, Any]:
    """Put a key downloaded from Apple where xcodebuild reads it, and tell the project about it.

    The file named must be an AuthKey_<id>.p8 in Downloads or on the Desktop (or already in a secure folder). It is copied,
    never moved: the original stays where Apple's download put it, and the result says so."""
    match = KEY_FILE.fullmatch(filename or "")
    if not match:
        raise SecretError("That isn't an Apple key file name. It looks like AuthKey_ABC123DEFG.p8.")
    key_id = match.group(1)
    issuer = (issuer or "").strip()
    if not ISSUER_ID.fullmatch(issuer):
        raise SecretError("The Issuer ID is a long code like 69a6de7e-8b0c-47e3-e053-5b8c7c11a4d1. It's shown at the top of the App Store Connect Keys page.")
    home = _home()
    source = next((home / folder / filename for folder in DOWNLOAD_FOLDERS if (home / folder / filename).is_file()), None)
    if source is None:
        source = next((folder / filename for folder in secure_key_dirs() if (folder / filename).is_file()), None)
    if source is None:
        raise SecretError(f"{filename} isn't in Downloads or on the Desktop of this computer. Download it from App Store Connect first (Apple only offers it once).")
    if source.is_symlink():
        raise SecretError("That file is a link, not the key itself. Download the key again.")
    data = source.read_bytes()
    if not _looks_like_apple_key(data):
        raise SecretError("That file doesn't look like an Apple API key (.p8). Nothing was changed.")
    destination = secure_key_dirs()[0] / filename
    say(f"Putting {filename} in ~/.private_keys (owner-only) …")
    _write_local(destination, data, replace=replace, new_dir_mode=0o700)
    say("  Checked: the copy is identical to the download.")
    update_project_json(root, {"asc_key_id": key_id, "asc_issuer_id": issuer, "asc_key_path": f"~/.private_keys/{filename}"})
    say("  Saved the key id, issuer and location in this project's settings (the key itself is not in them).")
    problems = _distribution_problems(root)
    return {"key_id": key_id, "file": filename, "original": f"~/{source.parent.name}/{filename}" if source.parent.parent == home else str(source),
            "original_left": source != destination, "problems": problems}


def _distribution_problems(root: Path) -> list[str]:
    try:
        return project_config.load_project_config(root).validate_distribution_config()
    except Exception as exc:  # a half-written project shouldn't turn a saved key into an error
        return [f"The project's settings couldn't be checked: {exc}"]


def secure_ios_key(root: Path) -> dict[str, Any]:
    """Make this project's key file owner-only (600). Only the key file the project names, only if it's in a secure folder."""
    config = _read_json(runtime_dir(root) / "project.json")
    path = locate_asc_key(str(config.get("asc_key_path") or ""), str(config.get("asc_key_id") or ""), root)
    if path is None or path.parent not in secure_key_dirs() or not path.is_file() or path.is_symlink():
        raise SecretError("The key isn't in a secure folder on this computer, so there is nothing to fix here.")
    path.chmod(0o600)
    return {"file": path.name}


# ---------------------------------------------------------------------------------------------------------- Android

_BLOCK_OPEN = re.compile(r"\{")
RELEASE_IN_SIGNING = re.compile(r"""\brelease\b\s*[{(]|create\(\s*["']release["']\s*\)|getByName\(\s*["']release["']\s*\)|\[\s*["']release["']\s*\]""")
PROPERTIES_FILE = re.compile(r"""["']([\w./-]*\.properties)["']""")
ENV_VAR = re.compile(r"""(?:System\.getenv|providers\.environmentVariable)\(\s*["'](\w+)["']""")
STORE_FILE = re.compile(r"""\bstoreFile\b\s*=?\s*(.+)""")
PROPERTY_SKIP = {"local.properties", "gradle.properties", "gradle-wrapper.properties"}


def _block(text: str, name: str) -> str | None:
    """The text inside `name { ... }`, braces matched."""
    match = re.search(rf"\b{name}\s*\{{", text)
    if not match:
        return None
    depth, start = 1, match.end()
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    return None


def _android_modules(root: Path) -> list[Path]:
    """Folders (up to three deep) whose Gradle file applies the Android application plugin."""
    modules: list[Path] = []

    def walk(folder: Path, depth: int) -> None:
        if depth > 3 or len(modules) >= 6:
            return
        for name in ("build.gradle", "build.gradle.kts"):
            gradle = folder / name
            try:
                if gradle.is_file() and re.search(r"com\.android\.application", gradle.read_text(encoding="utf-8", errors="ignore")[:200_000]):
                    modules.append(folder)
                    break
            except OSError:
                pass
        try:
            children = sorted(p for p in folder.iterdir() if p.is_dir() and not p.is_symlink() and p.name not in SKIP_DIRS and not p.name.startswith("."))
        except OSError:
            return
        for child in children[:60]:
            walk(child, depth + 1)

    walk(root, 0)
    return modules


def _gradle_root(module: Path, root: Path) -> Path:
    """The folder Gradle calls rootProject: the nearest one above the module that has a settings file."""
    folder = safe_resolve(module)
    limit = safe_resolve(root)
    while True:
        if any((folder / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")):
            return folder
        if folder == limit or folder.parent == folder:
            return limit
        folder = folder.parent


def _properties_value(path: Path, key: str) -> str:
    """One value from a .properties file. Only the key asked for is read out; passwords in the same file are never touched."""
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[:200]:
            name, sep, value = line.partition("=")
            if sep and name.strip() == key:
                return value.strip()
    except OSError:
        pass
    return ""


def _resolve(path_text: str, *bases: Path) -> Path | None:
    candidate = Path(path_text).expanduser()
    if candidate.is_absolute():
        return candidate
    for base in bases:
        if (base / candidate).exists():
            return safe_resolve(base / candidate)
    return safe_resolve(bases[0] / candidate) if bases else None


def _relative_to_root(path: Path, root: Path) -> str | None:
    try:
        return safe_resolve(path).relative_to(safe_resolve(root)).as_posix()
    except ValueError:
        return None


def _file_entry(kind: str, label: str, path: Path | None, root: Path) -> dict[str, Any]:
    """One secret file the project depends on. `where`/`rel` say how to find it on another computer."""
    home = _home()
    entry: dict[str, Any] = {"kind": kind, "label": label, "name": path.name if path else "", "here": False, "where": "", "rel": ""}
    if path is None:
        return entry
    entry["here"] = path.is_file() and not path.is_symlink()
    in_project = _relative_to_root(path, root)
    if in_project is not None and ".." not in Path(in_project).parts:
        entry.update(where="project", rel=in_project)
    else:
        try:
            entry.update(where="home", rel=safe_resolve(path).relative_to(home).as_posix())
        except ValueError:
            entry["other_home"] = bool(re.match(r"^/(Users|home)/[^/]+/", str(path)))
    entry["copyable"] = entry["where"] in ("home", "project")  # only files the project or home folder can name are ever copied
    if entry["copyable"]:
        entry["id"] = _sha256(f"{kind}:{entry['where']}:{entry['rel']}:{entry['name']}".encode())[:10]
    return entry


def android_applies(config: dict[str, Any]) -> bool:
    platform = platform_of(config)
    if platform == "android":
        return True
    return platform == "file" and not str(config.get("delivery_artifact") or "").lower().endswith(".ipa")


def android_view(config: dict[str, Any], root: Path) -> dict[str, Any] | None:
    """Whether the Android release keystore (and the settings file that names it) is on this computer.

    Best effort over the Gradle files, never a guess presented as fact: anything it can't read ends as "unknown"."""
    if not android_applies(config):
        return None
    view: dict[str, Any] = {"platform": "android", "label": "Android signing key", "files": []}
    modules = _android_modules(root)
    if not modules:
        view["state"] = "no-project"
        return view
    for module in modules:
        gradle = next((module / n for n in ("build.gradle", "build.gradle.kts") if (module / n).is_file()), None)
        text = gradle.read_text(encoding="utf-8", errors="ignore")[:400_000] if gradle else ""
        signing = _block(text, "signingConfigs")
        if not signing or not RELEASE_IN_SIGNING.search(signing):
            continue
        env_names = sorted(set(ENV_VAR.findall(signing)))
        files: list[dict[str, Any]] = []
        property_files = [name for name in PROPERTIES_FILE.findall(text) if Path(name).name not in PROPERTY_SKIP]
        store = STORE_FILE.search(signing)
        store_expr = store.group(1) if store else ""
        literal = re.search(r"""file\(\s*["']([^"']+)["']\s*\)""", store_expr)
        if literal and not property_files:
            base = _gradle_root(module, root) if "rootProject.file" in store_expr else module
            files.append(_file_entry("keystore", "Release keystore", _resolve(literal.group(1), base, root), root))
        elif property_files:
            gradle_root = _gradle_root(module, root)
            for name in dict.fromkeys(property_files):
                from_root = re.search(rf"""(?:rootProject\.file\(|new File\(\s*rootDir\s*,)\s*["']{re.escape(name)}["']""", text)
                props = _resolve(name, *((gradle_root, module) if from_root else (module, gradle_root)))
                files.append(_file_entry("settings", f"Signing settings ({Path(name).name})", props, root))
                if props is not None and props.is_file():
                    store_path = _properties_value(props, "storeFile")
                    if store_path:
                        files.append(_file_entry("keystore", "Release keystore", _resolve(store_path, module, props.parent, root), root))
        elif env_names:
            view.update(state="env", env=env_names)
            return view
        else:
            view["state"] = "unknown"
            return view
        view["files"] = files
        if not files:
            view["state"] = "unknown"
        elif all(f["here"] for f in files):
            view["state"] = "ready"
        else:
            view["state"] = "missing"
            view["on_another_computer"] = any(f.get("other_home") for f in files if not f["here"])
        return view
    view["state"] = "not-needed"  # release builds use the debug key, which is fine for tester builds
    return view


# -------------------------------------------------------------------------------------------- where each secret is

def status(config: dict[str, Any], root: Path, projects: list[tuple[str, Path]] | None = None) -> dict[str, Any]:
    """Everything the delivery page shows about signing, from this computer only (no network)."""
    ios, android = ios_view(config, root), android_view(config, root)
    if ios and ios["state"] in ("none", "partial", "missing"):
        ios["downloads"] = downloaded_keys()
        ios["issuer_hints"] = issuer_hints(projects or [], root) if ios["state"] != "missing" or not config.get("asc_issuer_id") else []
    return {"ios": ios, "android": android}


# -------------------------------------------------------------------------------------------- moving a secret over SSH

@dataclass
class Machine:
    """This computer (no target) or another one reached over SSH, with its copy of the project."""
    name: str
    target: str | None = None
    repo_path: str | None = None


def machines_from_config(root: Path) -> list[Machine]:
    raw = _read_json(runtime_dir(root) / "config" / "machines.json").get("machines", [])
    out: list[Machine] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("execution_mode") == "ssh" and item.get("ssh_target") and item.get("repo_path") and item.get("name"):
            target = item["ssh_target"][0] if isinstance(item["ssh_target"], list) else item["ssh_target"]
            if re.fullmatch(r"[A-Za-z0-9._@:\-\[\]]+", str(target)) and not str(target).startswith("-"):
                out.append(Machine(str(item["name"]), str(target), str(item["repo_path"])))
    return out


@dataclass
class Secret:
    """One secret file: `where` is "home" (rel to the home folder) or "project" (rel to the project folder)."""
    kind: str
    label: str
    where: str
    rel: str
    max_bytes: int

    def check(self, data: bytes) -> None:
        if len(data) > self.max_bytes or not data:
            raise SecretError("That file isn't the size a signing file should be, so it was not copied.")
        if self.kind == "ios-key" and not _looks_like_apple_key(data):
            raise SecretError("The file there doesn't look like an Apple API key, so it was not copied.")

    def parts(self) -> list[str]:
        pieces = [p for p in self.rel.split("/") if p]
        if not pieces or any(p in (".", "..") for p in pieces) or self.rel.startswith("/") or self.where not in ("home", "project"):
            raise SecretError("That location isn't allowed for a signing file.")
        return pieces


def ios_secret(key_id: str, folder: str = ".private_keys") -> Secret:
    if not KEY_ID.fullmatch(key_id or ""):
        raise SecretError("This project has no valid App Store Connect key id yet.")
    if folder not in (".private_keys", ".appstoreconnect/private_keys"):
        raise SecretError("That location isn't allowed for a signing file.")
    return Secret("ios-key", "iOS signing key", "home", f"{folder}/AuthKey_{key_id}.p8", IOS_KEY_MAX)


def android_secret(entry: dict[str, Any]) -> Secret:
    if entry.get("where") not in ("home", "project") or not entry.get("rel"):
        raise SecretError("That file can't be copied by name (its path isn't in this project or your home folder).")
    return Secret(f"android-{entry['kind']}", entry["label"], entry["where"], entry["rel"], ANDROID_FILE_MAX)


def _remote_path(secret: Secret, machine: Machine) -> str:
    tail = "/".join(shlex.quote(p) for p in secret.parts())
    if secret.where == "home":
        return f'"$HOME"/{tail}'
    return f"{shlex.quote((machine.repo_path or '').rstrip('/'))}/{tail}"


def _local_path(secret: Secret, root: Path) -> Path:
    base = _home() if secret.where == "home" else safe_resolve(root)
    path = base.joinpath(*secret.parts())
    try:  # a folder in the middle that is really a link out of the project or home must not carry the file somewhere else
        safe_resolve(path.parent).relative_to(base)
    except ValueError:
        raise SecretError("That location isn't allowed for a signing file.") from None
    return path


def _ssh(machine: Machine, command: str, run: Run, data: bytes | None = None, timeout: int = SSH_TIMEOUT) -> tuple[int, bytes, bytes]:
    return run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", str(machine.target), command], data, timeout)


def has_secret(secret: Secret, machine: Machine, root: Path, run: Run = run_process) -> bool | None:
    """True / False, or None when the computer couldn't be reached."""
    if machine.target is None:
        path = _local_path(secret, root)
        return path.is_file() and not path.is_symlink()
    code, _, _ = _ssh(machine, f'p={_remote_path(secret, machine)}; test -f "$p" && ! test -L "$p"', run, timeout=15)
    return True if code == 0 else (False if code == 1 else None)


def _digest(secret: Secret, machine: Machine, root: Path, run: Run) -> str | None:
    if machine.target is None:
        path = _local_path(secret, root)
        return _sha256(path.read_bytes()) if path.is_file() else None
    code, out, _ = _ssh(machine, f'p={_remote_path(secret, machine)}; [ -f "$p" ] || exit 1; (shasum -a 256 -- "$p" || sha256sum -- "$p") 2>/dev/null | cut -d" " -f1', run, timeout=20)
    text = out.decode(errors="ignore").strip()
    return text if code == 0 and re.fullmatch(r"[0-9a-f]{64}", text) else None


def _read(secret: Secret, machine: Machine, root: Path, run: Run) -> bytes:
    if machine.target is None:
        path = _local_path(secret, root)
        if path.is_symlink() or not path.is_file():
            raise SecretError(f"{secret.label} isn't on this computer.")
        if path.stat().st_size > secret.max_bytes:
            raise SecretError("That file isn't the size a signing file should be, so it was not copied.")
        return path.read_bytes()
    code, out, _ = _ssh(machine, f'p={_remote_path(secret, machine)}; [ -f "$p" ] && [ ! -L "$p" ] || exit 4; [ "$(wc -c < "$p")" -le {secret.max_bytes} ] || exit 5; cat -- "$p"', run)
    if code == 4:
        raise SecretError(f"{secret.label} isn't on {machine.name}.")
    if code == 5:
        raise SecretError("That file isn't the size a signing file should be, so it was not copied.")
    if code != 0:
        raise SecretError(f"Couldn't reach {machine.name} over SSH. Check that you can ssh to it without a password prompt.")
    return out


def _write_local(path: Path, data: bytes, *, replace: bool, new_dir_mode: int = 0o700) -> None:
    if path.is_symlink():
        raise SecretError("A link is already at that location, so nothing was written.")
    if path.exists():
        if _sha256(path.read_bytes()) == _sha256(data):
            return
        if not replace:
            raise SecretError("A different file is already there, so nothing was changed. Replace it only if you mean to.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=new_dir_mode)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _ignored_here(root: Path, rel: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", rel], capture_output=True).returncode == 0


def _write(secret: Secret, machine: Machine, root: Path, data: bytes, run: Run, replace: bool) -> None:
    if secret.where == "project":  # a secret inside a project folder must never be one `git add` away from being committed
        if machine.target is None:
            if not _ignored_here(root, secret.rel):
                raise SecretError(f"{secret.rel} isn't git-ignored in this project, so it would be committed. Add it to .gitignore first.")
        else:
            code, _, _ = _ssh(machine, f"cd {shlex.quote(machine.repo_path or '')} && git check-ignore -q -- {shlex.quote(secret.rel)}", run, timeout=15)
            if code != 0:
                raise SecretError(f"{secret.rel} isn't git-ignored on {machine.name}, so it would be committed there. Add it to .gitignore first.")
    existing = _digest(secret, machine, root, run)
    if existing == _sha256(data):
        return
    if existing and not replace:
        raise SecretError(f"A different file is already on {machine.name if machine.target else 'this computer'}, so nothing was changed. Replace it only if you mean to.")
    if machine.target is None:
        _write_local(_local_path(secret, root), data, replace=replace)
        return
    path = _remote_path(secret, machine)
    command = (f'umask 077; d={path}; [ ! -L "$d" ] || exit 3; mkdir -p -- "$(dirname -- "$d")" && t="$d.tmp.$$" && '
               f'cat > "$t" && chmod 600 "$t" && mv -f -- "$t" "$d"')
    code, _, _ = _ssh(machine, command, run, data)
    if code == 3:
        raise SecretError("A link is already at that location on the other computer, so nothing was written.")
    if code != 0:
        raise SecretError(f"Couldn't write the file on {machine.name}. Check that you can ssh to it without a password prompt.")


def copy_secret(secret: Secret, source: Machine, destination: Machine, root: Path, *, replace: bool = False, run: Run = run_process,
                say: Callable[[str], None] = print, destination_secret: Secret | None = None) -> dict[str, Any]:
    """Copy one signing file between this computer and one SSH computer. Nothing secret is printed; the result is checked by hash.

    `destination_secret` is where it lands when that differs from where it was (an iOS key found in one secure folder lands in the other)."""
    target = destination_secret or secret
    if (source.target is None) == (destination.target is None):
        raise SecretError("A copy goes between this computer and one other computer.")
    say(f"Reading {secret.label} from {source.name if source.target else 'this computer'} …")
    data = _read(secret, source, root, run)
    secret.check(data)
    expected = _sha256(data)
    if source.target is not None and _digest(secret, source, root, run) != expected:
        raise SecretError("The file changed or was cut short while being read, so nothing was copied.")
    say(f"Writing it to {destination.name if destination.target else 'this computer'} (owner-only) …")
    _write(target, destination, root, data, run, replace)
    if _digest(target, destination, root, run) != expected:
        raise SecretError("The copy doesn't match the original, so don't use it. Try again.")
    say("  Checked: the copy is identical to the original.")
    return {"copied": True, "label": secret.label, "to": destination.name if destination.target else "this computer"}


def find_elsewhere(secrets: list[Secret], machines: list[Machine], root: Path, run: Run = run_process) -> list[dict[str, Any]]:
    """For each SSH computer: does it have all of these files? (True / False / None when unreachable)."""
    out = []
    for machine in machines:
        states = [has_secret(secret, machine, root, run) for secret in secrets]
        out.append({"name": machine.name, "has": None if None in states else all(states)})
    return out


def ios_secret_on(machine: Machine, key_id: str, root: Path, run: Run = run_process) -> Secret | None:
    """The key's file on that computer (this one when `machine` has no target), whichever of the two secure folders holds it."""
    for folder in (".private_keys", ".appstoreconnect/private_keys"):
        secret = ios_secret(key_id, folder)
        if has_secret(secret, machine, root, run):
            return secret
    return None


def find_ios_elsewhere(key_id: str, machines: list[Machine], root: Path, run: Run = run_process) -> list[dict[str, Any]]:
    """For each SSH computer: has it the key? (True / False / None when it couldn't be reached)."""
    out = []
    for machine in machines:
        states = [has_secret(ios_secret(key_id, folder), machine, root, run) for folder in (".private_keys", ".appstoreconnect/private_keys")]
        out.append({"name": machine.name, "has": True if True in states else (None if None in states else False)})
    return out
