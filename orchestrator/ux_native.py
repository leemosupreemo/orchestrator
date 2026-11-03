"""Screenshots of native apps for the UX review, beside the web capture in ux_review.py.

- the Android emulator (or a device), through adb: install the build, launch it, open each deep link, capture;
- a macOS app: build, launch, capture its window with screencapture;
- (extra iOS screens are opened by deep link in scripts/simulator_visual_check.py, which already owns the simulator.)

Each capture returns (shots, problem) like ux_review.capture_web: shots are {file, route, width, dark} dicts saved in
out_dir and problem is "" when it worked. Process-running pieces are passed in so tests don't need a device.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

BOOT_TIMEOUT = 180
PNG_MAGIC = b"\x89PNG"

# Prints the id of the largest on-screen window owned by the given process ids, for `screencapture -l`.
WINDOW_ID_SCRIPT = """ObjC.import('CoreGraphics');
const pids = %s;
const info = ObjC.deepUnwrap(ObjC.castRefToObject($.CGWindowListCopyWindowInfo($.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements, 0)));
let best = null;
for (const w of info) {
  if (!pids.includes(w.kCGWindowOwnerPID) || w.kCGWindowLayer !== 0) continue;
  const area = w.kCGWindowBounds.Width * w.kCGWindowBounds.Height;
  if (!best || area > best.area) best = {id: w.kCGWindowNumber, area};
}
best ? String(best.id) : ''"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "screen"


def find_adb(env: dict[str, str] | None = None, which: Callable[[str], str | None] = shutil.which) -> str | None:
    env = os.environ if env is None else env
    found = which("adb")
    if found:
        return found
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        candidate = Path(env.get(key) or "/nonexistent") / "platform-tools" / "adb"
        if candidate.is_file():
            return str(candidate)
    default = Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"
    return str(default) if default.is_file() else None


def find_emulator(adb: str, which: Callable[[str], str | None] = shutil.which) -> str | None:
    sibling = Path(adb).resolve().parent.parent / "emulator" / "emulator"
    return str(sibling) if sibling.is_file() else which("emulator")


def running_device(devices_output: str) -> str | None:
    """The serial of a booted device from `adb devices`, preferring an emulator."""
    serials = [parts[0] for parts in (line.split() for line in devices_output.splitlines()[1:]) if len(parts) >= 2 and parts[1] == "device"]
    return next((s for s in serials if s.startswith("emulator-")), serials[0] if serials else None)


def _tail(text: str | bytes | None) -> str:
    text = text.decode("utf-8", "replace") if isinstance(text, bytes) else (text or "")
    return text.strip().splitlines()[-1][:200] if text.strip() else ""


def capture_android(root: Path, cfg: dict[str, Any], out_dir: Path, *, dark_mode: bool = True, log: Callable[[str], Any] = print,
                    run: Callable[..., Any] = subprocess.run, popen: Callable[..., Any] = subprocess.Popen,
                    sleep: Callable[[float], Any] = time.sleep, adb: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Launch the app on a running emulator or device (starting `avd` if none is running) and screenshot it, at launch and at
    each deep link, in light and dark."""
    adb = adb or find_adb()
    if not adb:
        return [], "adb wasn't found. Install the Android platform-tools (or set ANDROID_HOME) to capture the Android emulator."
    package, started = cfg["package"], None

    def adb_run(serial: str | None, *args: str, timeout: float = 60, binary: bool = False) -> Any:
        return run([adb, *(["-s", serial] if serial else []), *args], capture_output=True, text=not binary, timeout=timeout, check=False)

    serial = running_device(adb_run(None, "devices").stdout or "")
    if not serial:
        emulator = find_emulator(adb) if cfg.get("avd") else None
        if not cfg.get("avd"):
            return [], "No Android emulator or device is running. Start one, or set ui_review.android.avd so Orchestrator can."
        if not emulator:
            return [], "The Android emulator program wasn't found next to adb, so the avd can't be started."
        log(f"Starting the Android emulator: {cfg['avd']}")
        started = popen([emulator, "-avd", cfg["avd"], "-no-snapshot-save", "-no-audio", "-no-boot-anim", "-no-window"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            serial = running_device(adb_run(None, "devices").stdout or "")
            if serial and (adb_run(serial, "shell", "getprop", "sys.boot_completed").stdout or "").strip() == "1":
                break
            serial = None
            sleep(3)
        if not serial:
            started.terminate()
            return [], f"The emulator {cfg['avd']} didn't finish starting within {BOOT_TIMEOUT} seconds."

    shots: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        if cfg.get("build_command"):
            log(f"Building the Android app: {cfg['build_command']}")
            built = run(cfg["build_command"], cwd=root, shell=True, capture_output=True, text=True, check=False)
            if built.returncode:
                return [], f"The Android build failed: {_tail(built.stderr or built.stdout)}"
        if cfg.get("apk"):
            apk = (root / os.path.expandvars(cfg["apk"])).resolve()
            if not apk.is_file():
                return [], f"The Android build wasn't found at {cfg['apk']}."
            installed = adb_run(serial, "install", "-r", "-t", str(apk), timeout=180)
            if installed.returncode:
                return [], f"Installing the Android build failed: {_tail(installed.stderr or installed.stdout)}"
        out_dir.mkdir(parents=True, exist_ok=True)
        for dark in ([False, True] if dark_mode else [False]):
            adb_run(serial, "shell", "cmd", "uimode", "night", "yes" if dark else "no")
            adb_run(serial, "shell", "am", "force-stop", package)
            adb_run(serial, "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
            sleep(float(cfg.get("wait") or 4))
            targets = [("Android, after launch", "launch")] + [(url, url) for url in cfg.get("screens", [])]
            for index, (label, name) in enumerate(targets):
                if index:
                    adb_run(serial, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", label, package)
                    sleep(2.5)
                shot = adb_run(serial, "exec-out", "screencap", "-p", binary=True, timeout=30)
                if shot.returncode or not (shot.stdout or b"").startswith(PNG_MAGIC):
                    failures.append(f"{label}{' dark' if dark else ''}")
                    log(f"  couldn't capture {label}")
                    continue
                file = f"android-{slug(name)}-{'dark' if dark else 'light'}.png"
                (out_dir / file).write_bytes(shot.stdout)
                shots.append({"file": file, "route": label, "width": 0, "dark": dark})
                log(f"  captured {label}{' dark' if dark else ''}")
    finally:
        adb_run(serial, "shell", "cmd", "uimode", "night", "auto")
        if started:
            adb_run(serial, "emu", "kill")
            started.terminate()
    return shots, ("Missing screenshots: " + ", ".join(failures)) if failures else "" if shots else "The Android device saved no screenshots."


def capture_macos(root: Path, cfg: dict[str, Any], out_dir: Path, *, log: Callable[[str], Any] = print,
                  run: Callable[..., Any] = subprocess.run, sleep: Callable[[float], Any] = time.sleep) -> tuple[list[dict[str, Any]], str]:
    """Launch a macOS app and screenshot its window. The terminal or app running Orchestrator needs Screen Recording
    permission, or the capture is blank or refused."""
    if cfg.get("build_command"):
        log(f"Building the Mac app: {cfg['build_command']}")
        built = run(cfg["build_command"], cwd=root, shell=True, capture_output=True, text=True, check=False)
        if built.returncode:
            return [], f"The Mac build failed: {_tail(built.stderr or built.stdout)}"
    app = (root / os.path.expandvars(cfg["app"])).resolve()
    if not app.is_dir():
        return [], f"The Mac app wasn't found at {cfg['app']}."
    executables = f"{app}/Contents/MacOS/"
    run(["pkill", "-f", executables], capture_output=True, check=False)  # a copy left from before would hide the new window
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        log(f"Launching {app.name}")
        if run(["open", "-n", str(app)], capture_output=True, text=True, check=False).returncode:
            return [], f"{app.name} couldn't be opened."
        sleep(float(cfg.get("wait") or 4))
        pids = [int(p) for p in (run(["pgrep", "-f", executables], capture_output=True, text=True, check=False).stdout or "").split()]
        if not pids:
            return [], f"{app.name} isn't running after launch. It may have quit or crashed."
        window = (run(["osascript", "-l", "JavaScript", "-e", WINDOW_ID_SCRIPT % pids], capture_output=True, text=True, check=False).stdout or "").strip()
        if not window.isdigit():
            return [], f"{app.name} has no visible window to capture."
        target = out_dir / f"macos-{slug(app.stem)}-light.png"
        shot = run(["screencapture", "-x", "-o", "-l", window, str(target)], capture_output=True, text=True, check=False)
        if shot.returncode or not target.is_file() or not target.stat().st_size:
            return [], ("The window couldn't be captured. Allow Screen Recording for the app running Orchestrator in "
                        "System Settings → Privacy & Security.")
        log(f"  captured {app.stem}")
        return [{"file": target.name, "route": f"{app.stem} window", "width": 0, "dark": False}], ""
    finally:
        run(["pkill", "-f", executables], capture_output=True, check=False)
