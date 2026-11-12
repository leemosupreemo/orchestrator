"""Where a finished build goes, per platform.

`delivery_platform` in `.orchestrator/project.json` picks the adapter:

- `ios` (the default): the project's own distribution script archives and uploads through Firebase App Distribution.
  That flow lives in scripts/deliver_build.py; this adapter only says it is the iOS one.
- `android`: build (optional `delivery_build_command`), find the newest APK/AAB matching `delivery_artifact`, and upload
  it with `firebase appdistribution:distribute` to `firebase_app_id`, for `firebase_groups` / `firebase_testers`.
- `web`: build (optional `delivery_build_command`), then `firebase hosting:channel:deploy` a preview channel named after
  the job, which expires after 7 days. Testers get the preview URL.

Both Firebase adapters need the `firebase` CLI signed in (`firebase login`) or `FIREBASE_TOKEN` set. The pure parts
(validation, the command lines, reading the result) are separate from the part that runs them, so they can be tested
without Firebase.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PLATFORMS = ("ios", "android", "web")
ARTIFACT_SUFFIXES = (".apk", ".aab")
DEFAULT_GROUP = "internal-testers"
BUILD_TIMEOUT = 60 * 60
UPLOAD_TIMEOUT = 30 * 60
PREVIEW_EXPIRES = "7d"
CONSOLE_URL = re.compile(r"https://console\.firebase\.google\.com/\S+")
URL = re.compile(r"https://[A-Za-z0-9.-]+\.(?:web\.app|firebaseapp\.com)\S*")

Run = Callable[[list[str], Path, int], "tuple[int, str]"]  # argv, cwd, timeout -> (exit code, output)


def platform_of(config: Any) -> str:
    return str(getattr(config, "delivery_platform", None) or "ios").strip().lower()


def recipients(config: Any, job: dict[str, Any]) -> tuple[str | None, str | None]:
    """(testers, groups): the job's choice, else the project's, else the default group."""
    testers = job.get("testers") or getattr(config, "firebase_testers", None)
    groups = job.get("groups") or getattr(config, "firebase_groups", None)
    if not testers and not groups:
        groups = DEFAULT_GROUP
    return testers, groups


def channel_id(job_id: str) -> str:
    """A Hosting channel name from a job id: lowercase letters, digits and dashes."""
    name = re.sub(r"[^a-z0-9]+", "-", str(job_id or "").lower()).strip("-")[:40].strip("-")
    return name or "preview"


def find_artifact(root: Path, pattern: str, since: float | None = None) -> Path | None:
    """The newest APK/AAB matching a glob inside the project. With `since`, one older than that is stale and ignored."""
    root = root.resolve()
    found = []
    for path in root.glob(pattern):
        try:
            inside = path.resolve().is_relative_to(root)
            if inside and path.is_file() and path.suffix.lower() in ARTIFACT_SUFFIXES \
                    and (since is None or path.stat().st_mtime >= since):
                found.append(path)
        except (OSError, ValueError):
            continue
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _project_args(config: Any) -> list[str]:
    project = getattr(config, "firebase_project_id", None)
    return ["--project", project] if project else []


def _glob_error(field: str, pattern: str) -> str | None:
    if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
        return f"{field} must be a path or glob inside the project."
    return None


@dataclass(frozen=True)
class Adapter:
    platform: str
    label: str

    def validate(self, config: Any) -> list[str]:
        return []

    def command(self, config: Any, job: dict[str, Any], notes: str, artifact: Path | None) -> list[str]:
        raise NotImplementedError

    def receipt(self, config: Any, job: dict[str, Any], output: str, artifact: Path | None) -> dict[str, Any]:
        """What reached testers, read from the command's output. Empty when the output doesn't show delivery."""
        raise NotImplementedError


class IosAdapter(Adapter):
    """Delivered by the project's distribution script (scripts/deliver_build.py); nothing to build here."""


class AndroidAdapter(Adapter):
    def validate(self, config: Any) -> list[str]:
        errors = []
        if not getattr(config, "firebase_app_id", None):
            errors.append("delivery_platform 'android' requires firebase_app_id (Firebase console > Project settings > "
                          "Your apps, like 1:1234567890:android:abc123).")
        artifact = getattr(config, "delivery_artifact", None)
        if not artifact:
            errors.append("delivery_platform 'android' requires delivery_artifact: the .apk or .aab to upload, "
                          "or a glob like Builds/*.apk.")
        elif (error := _glob_error("delivery_artifact", artifact)):
            errors.append(error)
        return errors

    def command(self, config, job, notes, artifact):
        testers, groups = recipients(config, job)
        argv = ["firebase", "appdistribution:distribute", str(artifact), "--app", config.firebase_app_id,
                "--release-notes", notes]
        if groups:
            argv += ["--groups", groups]
        if testers:
            argv += ["--testers", testers]
        return argv + _project_args(config)

    def receipt(self, config, job, output, artifact):
        if artifact is None:  # the firebase CLI exits non-zero when the upload or distribution fails
            return {}
        testers, groups = recipients(config, job)
        console = CONSOLE_URL.findall(output)
        return {"status": "delivered", "provider": "firebase", "platform": "android", "artifact": str(artifact),
                "sha256": sha256(artifact), "version": None, "build": None, "testers": testers, "groups": groups,
                "console_url": console[-1] if console else None, "confirmed_by": "firebase"}


class WebAdapter(Adapter):
    def validate(self, config: Any) -> list[str]:
        errors = []
        root = getattr(config, "root", None)
        if root is not None and not (Path(root) / "firebase.json").is_file():
            errors.append("delivery_platform 'web' needs a firebase.json in the project (run 'firebase init hosting').")
        return errors

    def command(self, config, job, notes, artifact):
        argv = ["firebase", "hosting:channel:deploy", channel_id(job.get("job_id", "")), "--expires", PREVIEW_EXPIRES]
        target = getattr(config, "firebase_hosting_target", None)
        if target:
            argv += ["--only", target]
        return argv + _project_args(config) + ["--json"]

    def receipt(self, config, job, output, artifact):
        url = _preview_url(output)
        if not url:
            return {}
        return {"status": "delivered", "provider": "firebase", "platform": "web", "preview_url": url, "version": None,
                "build": None, "testers": None, "groups": None, "recipients": url, "console_url": url,
                "confirmed_by": "firebase"}


def _preview_url(output: str) -> str | None:
    """The preview URL from `firebase hosting:channel:deploy --json` output, or from its plain text."""
    start = output.find("{")
    if start != -1:
        try:
            data = json.loads(output[start:output.rfind("}") + 1])
        except json.JSONDecodeError:
            data = None
        sites = (data or {}).get("result") if isinstance(data, dict) else None
        if isinstance(sites, dict):
            for site in sites.values():
                if isinstance(site, dict) and isinstance(site.get("url"), str):
                    return site["url"]
    found = URL.findall(output)
    return found[-1].rstrip(".,)") if found else None


ADAPTERS: dict[str, Adapter] = {
    "ios": IosAdapter("ios", "Firebase App Distribution (iOS)"),
    "android": AndroidAdapter("android", "Firebase App Distribution (Android)"),
    "web": WebAdapter("web", "Firebase Hosting preview"),
}


def adapter_for(config: Any) -> Adapter:
    """The adapter for the project's `delivery_platform`. An unknown platform is a config error caught by validate_platform."""
    return ADAPTERS.get(platform_of(config), ADAPTERS["ios"])


def validate_platform(config: Any) -> list[str]:
    platform = platform_of(config)
    if platform not in PLATFORMS:
        return [f"delivery_platform must be one of {', '.join(PLATFORMS)}."]
    errors = ADAPTERS[platform].validate(config)
    build = getattr(config, "delivery_build_command", None)
    if build is not None and not isinstance(build, str):
        errors.append("delivery_build_command must be a shell command.")
    return errors


def deliver(config: Any, job: dict[str, Any], notes: str, *, run: Run, now: Callable[[], float],
            which: Callable[[str], str | None] = shutil.which) -> tuple[dict[str, Any], str]:
    """Build and send for an Android or web project. Returns (receipt, log). An empty receipt means delivery isn't
    confirmed; the log says why. `run` is injected: it runs a command and returns (exit code, output)."""
    adapter = adapter_for(config)
    root = Path(config.root)
    log: list[str] = []
    if not which("firebase"):
        return {}, "The firebase command isn't installed (npm install -g firebase-tools), then run firebase login."
    started = now()
    build = (getattr(config, "delivery_build_command", None) or "").strip()
    if build:
        log.append(f"$ {build}")
        code, output = run(["/bin/bash", "-c", build], root, BUILD_TIMEOUT)
        log.append(output)
        if code != 0:
            return {}, "\n".join(log) + f"\nThe build command failed (exit {code}); nothing was sent."
    artifact = None
    if adapter.platform == "android":
        artifact = find_artifact(root, config.delivery_artifact, started if build else None)
        if artifact is None:
            return {}, "\n".join(log) + (f"\nNo .apk or .aab matching {config.delivery_artifact}"
                                         + (" was written by the build." if build else " exists. Set delivery_build_command to build it."))
    argv = adapter.command(config, job, notes, artifact)
    log.append("$ " + " ".join(argv[:3]) + " …")  # not the rest: release notes and tester emails stay out of logs
    code, output = run(argv, root, UPLOAD_TIMEOUT)
    log.append(output)
    if code != 0:
        return {}, "\n".join(log) + f"\nFirebase failed (exit {code})."
    receipt = adapter.receipt(config, job, output, artifact)
    if not receipt:
        log.append("Firebase finished without errors, but its output doesn't show the build was delivered.")
    return receipt, "\n".join(log)


# ---------------------------------------------------------------------------------------------- for the web UI

TARGET_KEYS = ("delivery_platform", "delivery_build_command", "delivery_artifact", "firebase_app_id",
               "firebase_project_id", "firebase_hosting_target")


def target_view(config: dict[str, Any]) -> dict[str, Any]:
    """The delivery target as saved in project.json, for the Delivery page's form."""
    view = {key: str(config.get(key) or "") for key in TARGET_KEYS}
    view["delivery_platform"] = view["delivery_platform"].strip().lower() or "ios"
    view["automatic"] = bool(config.get("firebase_distribution"))
    return view


def target_update(root: Path, body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(changes to write to project.json, what is wrong). Only the chosen platform's fields are kept, and iOS changes
    nothing but the platform: its signing settings come from the wizard. Choosing Android or web with automatic delivery
    on also turns on `firebase_distribution` with the Firebase provider."""
    from types import SimpleNamespace

    def text(key: str) -> str:
        return str(body.get(key) or "").strip()

    platform = text("delivery_platform").lower() or "ios"
    if platform not in PLATFORMS:
        return {}, [f"Choose one of: {', '.join(PLATFORMS)}."]
    changes: dict[str, Any] = {"delivery_platform": platform}
    keep = {"ios": (), "android": ("delivery_build_command", "delivery_artifact", "firebase_app_id", "firebase_project_id"),
            "web": ("delivery_build_command", "firebase_project_id", "firebase_hosting_target")}[platform]
    for key in TARGET_KEYS[1:]:
        changes[key] = text(key) if key in keep and text(key) else None
    if platform != "ios":
        changes["firebase_distribution"] = bool(body.get("automatic"))
        if changes["firebase_distribution"]:
            changes["delivery_provider"] = "firebase"
    errors = []
    if platform != "ios":
        errors = validate_platform(SimpleNamespace(root=root, **{k: v for k, v in changes.items() if k in TARGET_KEYS}))
    return changes, errors
