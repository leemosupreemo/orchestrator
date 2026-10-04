"""UX and design review: a checklist pass over the product's screens.

Two scopes share one checklist (`prompts/ux_reviewer.md`):

- a **product pass** looks at every configured screen, at phone and desktop widths, light and dark;
- a **change review** runs after a job whose diff touches the interface, and looks only at what it changed.

Screens are captured with a headless Chrome for web projects (`ui_review` in `.orchestrator/project.json`) or the
iOS simulator check; with neither, the review works from the code alone and says so. The model's answer is
normalised here so the web UI can rely on its shape. Findings are prompts to look, not gates: a job is never blocked.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

CHECKLIST = {
    "ux": ["ux.status", "ux.words", "ux.consistent-names", "ux.one-primary", "ux.no-duplicate-actions", "ux.disclosure",
           "ux.grouping", "ux.one-home", "ux.wayfinding", "ux.back", "ux.mobile-nav", "ux.empty-states", "ux.errors", "ux.decision-context", "ux.menus", "ux.destructive",
           "ux.forms", "ux.recognition", "ux.say-once", "ux.action-labels", "ux.long-content", "ux.counts"],
    "design": ["design.hierarchy", "design.tokens", "design.spacing", "design.alignment", "design.sizing",
               "design.consistency", "design.section-headers", "design.header-anatomy", "design.fields",
               "design.native-controls", "design.overlap", "design.responsive", "design.contrast", "design.dark-mode",
               "design.motion", "design.focus"],
}
CHECK_IDS = {i for ids in CHECKLIST.values() for i in ids}
STATUSES = ("pass", "fail", "partial", "n/a")
DEFAULT_WIDTHS = (390, 1440)
VIEWPORT_HEIGHT = {True: 844, False: 900}  # phone, wider
MAX_DIFF_CHARS = 60_000
MAX_CONVENTIONS_CHARS = 12_000

# What counts as interface code. Script files only count inside folders that usually hold UI.
UI_EXTS = {".html", ".htm", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte", ".astro",
           ".xib", ".storyboard", ".xaml"}
SCRIPT_EXTS = {".js", ".ts", ".mjs", ".dart", ".kt"}
UI_DIRS = {"static", "public", "components", "pages", "views", "screens", "ui", "web", "frontend", "app", "routes",
           "layouts", "styles", "templates", "widgets"}
SWIFT_UI = re.compile(r"(View|Screen|Cell|Controller|Sheet|Modal)\.swift$")


def frontend_files(paths: list[str], extra_globs: list[str] | None = None) -> list[str]:
    """The changed files that render the interface. `extra_globs` (from `ui_review.paths`) always count."""
    out = []
    for path in paths:
        p = PurePosixPath(path)
        parts = {part.lower() for part in p.parts[:-1]}
        if (p.suffix.lower() in UI_EXTS
                or (p.suffix.lower() in SCRIPT_EXTS and parts & UI_DIRS)
                or (p.suffix == ".swift" and (SWIFT_UI.search(p.name) or parts & {"views", "ui", "screens"}))
                or any(fnmatch.fnmatch(path, g) for g in extra_globs or [])):
            out.append(path)
    return out


class ConfigError(ValueError):
    pass


def settings(project: dict[str, Any]) -> dict[str, Any]:
    """`ui_review` from project.json with defaults filled in. Raises ConfigError for values that can't work."""
    raw = project.get("ui_review") or {}
    if not isinstance(raw, dict):
        raise ConfigError("ui_review must be an object.")
    url = str(raw.get("url") or "").strip()
    if url and not re.match(r"^https?://", url):
        raise ConfigError("ui_review.url must start with http:// or https://.")
    routes = raw.get("routes") or ["/"]
    if not isinstance(routes, list) or not all(isinstance(r, str) and r.strip() for r in routes):
        raise ConfigError("ui_review.routes must be a list of paths, like [\"/\", \"/settings\"].")
    widths = raw.get("widths") or list(DEFAULT_WIDTHS)
    if not isinstance(widths, list) or not all(isinstance(w, int) and 320 <= w <= 2560 for w in widths):
        raise ConfigError("ui_review.widths must be a list of pixel widths between 320 and 2560.")
    paths = raw.get("paths") or []
    if not isinstance(paths, list) or not all(isinstance(g, str) for g in paths):
        raise ConfigError("ui_review.paths must be a list of file patterns.")
    return {"url": url, "start_command": str(raw.get("start_command") or "").strip(), "routes": [r.strip() for r in routes][:40],
            "widths": widths[:4], "dark_mode": raw.get("dark_mode", True) is not False, "simulator": bool(raw.get("simulator")),
            "paths": paths, "conventions": str(raw.get("conventions") or "").strip(),
            "review_changes": raw.get("review_changes", True) is not False}


def conventions_text(root: Path, configured: str = "") -> tuple[str, str]:
    """(path, text) of the project's own UI rules: the configured file, else a likely-named doc. ("", "") if none."""
    candidates = [configured] if configured else []
    candidates += sorted(str(p.relative_to(root)) for pattern in ("docs/*ui*convention*.md", "docs/*design-system*.md",
                                                                   "docs/*style-guide*.md", "DESIGN.md")
                         for p in root.glob(pattern))
    for rel in candidates:
        path = (root / rel).resolve()
        if path.is_file() and path.is_relative_to(root.resolve()):
            return rel, path.read_text(encoding="utf-8", errors="replace")[:MAX_CONVENTIONS_CHARS]
    return "", ""


def screen_url(base: str, route: str) -> str:
    route = route.strip()
    if route.startswith(("http://", "https://")):
        return route
    return base.rstrip("/") + ("" if route.startswith(("/", "#", "?")) else "/") + route


def shot_name(route: str, width: int, dark: bool) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-") or "home"
    return f"{slug[:50]}-{width}-{'dark' if dark else 'light'}.png"


def find_browser() -> str | None:
    """A Chrome-family browser for headless screenshots: ORCHESTRATOR_BROWSER, then the usual places."""
    configured = os.environ.get("ORCHESTRATOR_BROWSER")
    if configured and Path(configured).exists():
        return configured
    for app in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
                "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
        if Path(app).exists():
            return app
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout):  # noqa: S310 (a local dev URL from project.json)
            return True
    except urllib.error.HTTPError:
        return True  # it answered; a 401 or 404 still means the app is up
    except (urllib.error.URLError, OSError, ValueError):
        return False


def capture_web(root: Path, cfg: dict[str, Any], out_dir: Path, *, widths: list[int] | None = None,
                dark: bool | None = None, routes: list[str] | None = None, log=print) -> tuple[list[dict[str, Any]], str]:
    """Screenshot each route at each width (and in dark mode). Returns (shots, problem); problem is "" when it worked.
    Starts `start_command` if the URL isn't answering, and stops it afterwards."""
    base = os.path.expandvars(cfg["url"])  # so a token can come from the environment, not project.json
    browser = find_browser()
    if not browser:
        return [], "No Chrome, Chromium or Edge found for screenshots. Install one, or set ORCHESTRATOR_BROWSER."
    started = None
    if not reachable(base):
        if not cfg["start_command"]:
            return [], f"{cfg['url']} isn't answering. Start the app, or set ui_review.start_command."
        log(f"Starting the app: {cfg['start_command']}")
        started = subprocess.Popen(cfg["start_command"], cwd=root, shell=True, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 90
        while time.time() < deadline and not reachable(base):
            time.sleep(2)
        if not reachable(base):
            _stop(started)
            return [], f"Started the app, but {cfg['url']} still isn't answering after 90 seconds."
    shots = []
    out_dir.mkdir(parents=True, exist_ok=True)
    schemes = [False, True] if (cfg["dark_mode"] if dark is None else dark) else [False]
    try:
        with tempfile.TemporaryDirectory(prefix="ux-review-browser-") as profile, Browser(browser, profile) as page:
            for route in routes or cfg["routes"]:
                for width in widths or cfg["widths"]:
                    for is_dark in schemes:
                        target = out_dir / shot_name(route, width, is_dark)
                        try:
                            target.write_bytes(page.screenshot(screen_url(base, route), width, is_dark))
                            shots.append({"file": target.name, "route": route, "width": width, "dark": is_dark})
                            log(f"  captured {route} at {width}px{' dark' if is_dark else ''}")
                        except BrowserError as exc:
                            log(f"  couldn't capture {route} at {width}px: {exc}")
    except BrowserError as exc:
        return shots, f"The browser couldn't be driven: {exc}"
    finally:
        if started:
            _stop(started)
    return shots, "" if shots else "The browser ran but saved no screenshots."


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


class BrowserError(RuntimeError):
    pass


class Browser:
    """Headless Chrome driven over the DevTools protocol on a pipe (fds 3 and 4): real phone viewports through
    device emulation (a desktop window can't be narrower than about 500 px), and dark mode through the
    `prefers-color-scheme` media feature. Standard library only; one browser for every screenshot."""

    def __init__(self, executable: str, profile: str, settle: float = 2.5) -> None:
        self.executable, self.profile, self.settle = executable, profile, settle
        self._id, self._buffer = 0, b""

    def __enter__(self) -> "Browser":
        to_chrome_r, self._to_chrome = os.pipe()
        self._from_chrome, from_chrome_w = os.pipe()

        args = ["--headless=new", "--remote-debugging-pipe", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={self.profile}", "about:blank"]
        # Chrome reads commands on fd 3 and writes replies on fd 4; the shell maps our pipe ends onto those numbers.
        self.process = subprocess.Popen(
            ["/bin/sh", "-c", f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}', self.executable, *args],
            pass_fds=(to_chrome_r, from_chrome_w), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        target = self.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
        self.call("Page.enable", session=True)
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.call("Browser.close", timeout=5)
        except BrowserError:
            pass
        for fd in (self._to_chrome, self._from_chrome):
            try:
                os.close(fd)
            except OSError:
                pass
        if self.process.poll() is None:
            _stop(self.process)

    def call(self, method: str, params: dict[str, Any] | None = None, *, session: bool = False, timeout: float = 30) -> dict[str, Any]:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = self.session
        try:
            os.write(self._to_chrome, json.dumps(message).encode() + b"\0")
        except OSError as exc:
            raise BrowserError(f"{method}: {exc}") from exc
        deadline = time.time() + timeout
        while True:
            while b"\0" not in self._buffer:
                remaining = deadline - time.time()
                if remaining <= 0 or not select.select([self._from_chrome], [], [], remaining)[0]:
                    raise BrowserError(f"{method} timed out")
                chunk = os.read(self._from_chrome, 1 << 20)
                if not chunk:
                    raise BrowserError("the browser closed")
                self._buffer += chunk
            raw, self._buffer = self._buffer.split(b"\0", 1)
            reply = json.loads(raw)
            if reply.get("id") == self._id:
                if "error" in reply:
                    raise BrowserError(f"{method}: {reply['error'].get('message')}")
                return reply.get("result") or {}

    def screenshot(self, url: str, width: int, dark: bool) -> bytes:
        phone = width < 700
        self.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": VIEWPORT_HEIGHT[phone],
                                                         "deviceScaleFactor": 2 if phone else 1, "mobile": phone}, session=True)
        self.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}]}, session=True)
        self.call("Page.navigate", {"url": url}, session=True)
        time.sleep(self.settle)  # single-page apps render after load; give them a moment
        data = self.call("Page.captureScreenshot", {"format": "png"}, session=True, timeout=60).get("data")
        if not data:
            raise BrowserError("no image came back")
        return base64.b64decode(data)


def build_prompt(checklist: str, *, scope: str, ask: str, conventions: tuple[str, str], shots: list[dict[str, Any]],
                 shot_dir: Path | None, code: str, limits: list[str]) -> str:
    shot_lines = "\n".join(f"- {shot_dir / s['file']} ({s['route']}, {s['width']} px{', dark' if s['dark'] else ''})"
                           for s in shots) if shots and shot_dir else "None."
    conv_path, conv_text = conventions
    return f"""{checklist}

## Scope

{scope}

## What was asked for

{ask or "Not stated."}

## Screenshots (open each file)

{shot_lines}

## The project's own UI conventions{f" ({conv_path})" if conv_path else ""}

{conv_text or "None written down; use the checklist."}

## Interface code

{code or "Not included; read the interface files in this repository as needed."}

## Known limits of this run

{chr(10).join(f"- {line}" for line in limits) or "- None."}
"""


def _json_block(text: str) -> Any:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    return json.loads(candidate)


def parse_result(text: str) -> dict[str, Any]:
    """The model's answer in a fixed shape. Unknown checklist ids and malformed findings are dropped; every
    checklist item appears once (missing ones as n/a, "not reported")."""
    try:
        raw = _json_block(text)
    except (ValueError, json.JSONDecodeError):
        raw = None
    if not isinstance(raw, dict):
        return {"summary": "", "checklist": [{"id": i, "status": "n/a", "note": "not reported"} for ids in CHECKLIST.values() for i in ids],
                "findings": [], "limits": "The reviewer's answer couldn't be read; see the raw answer.", "unreadable": True}
    seen = {}
    for item in raw.get("checklist") or []:
        if isinstance(item, dict) and item.get("id") in CHECK_IDS:
            status = str(item.get("status") or "").lower()
            seen[item["id"]] = {"id": item["id"], "status": status if status in STATUSES else "n/a", "note": str(item.get("note") or "")[:300]}
    checklist = [seen.get(i, {"id": i, "status": "n/a", "note": "not reported"}) for ids in CHECKLIST.values() for i in ids]
    findings = []
    for f in raw.get("findings") or []:
        if not isinstance(f, dict) or not str(f.get("problem") or "").strip():
            continue
        try:
            severity = max(0, min(4, int(f.get("severity", 2))))
        except (TypeError, ValueError):
            severity = 2
        item = f.get("item") if f.get("item") in CHECK_IDS else ""
        area = "design" if (item or str(f.get("area"))).startswith("design") else "ux"
        findings.append({"area": area, "item": item, "severity": severity,
                         **{k: str(f.get(k) or "").strip()[:600] for k in ("screen", "element", "problem", "fix")}})
    findings.sort(key=lambda f: -f["severity"])
    return {"summary": str(raw.get("summary") or "").strip()[:1200], "checklist": checklist, "findings": findings[:60],
            "limits": str(raw.get("limits") or "").strip()[:600]}


def counts(result: dict[str, Any]) -> dict[str, Any]:
    findings = result.get("findings") or []
    checks = result.get("checklist") or []
    return {"findings": len(findings), "major": sum(1 for f in findings if f["severity"] >= 3),
            "ux": sum(1 for f in findings if f["area"] == "ux"), "design": sum(1 for f in findings if f["area"] == "design"),
            "failed": sum(1 for c in checks if c["status"] == "fail"), "partial": sum(1 for c in checks if c["status"] == "partial"),
            "passed": sum(1 for c in checks if c["status"] == "pass")}


SEVERITY = {0: "cosmetic", 1: "minor", 2: "moderate", 3: "major", 4: "blocker"}


def finding_line(f: dict[str, Any]) -> str:
    where = " · ".join(part for part in (f["screen"] or "all screens", f["element"]) if part)
    item = f" (`{f['item']}`)" if f["item"] else ""
    fix = f" Fix: {f['fix']}" if f["fix"] else ""
    return f"- [ ] **{SEVERITY[f['severity']]}** · {where}{item}: {f['problem']}{fix}"


def report_markdown(result: dict[str, Any], *, title: str, scope: str, shots: list[dict[str, Any]], when: str) -> str:
    c = counts(result)
    lines = [f"# {title}", "", f"{scope} · {when}", "",
             f"{c['findings']} findings ({c['major']} major or worse) · checklist: {c['passed']} pass, {c['partial']} partly, {c['failed']} fail", ""]
    if result.get("summary"):
        lines += [result["summary"], ""]
    for area, heading in (("ux", "UX"), ("design", "Design")):
        rows = [f for f in result["findings"] if f["area"] == area]
        lines += [f"## {heading} findings", ""]
        lines += [finding_line(f) for f in rows] or ["None."]
        lines.append("")
    lines += ["## Checklist", "", "| Item | Result | Note |", "|---|---|---|"]
    lines += [f"| `{c_['id']}` | {c_['status']} | {c_['note'].replace('|', '/')} |" for c_ in result["checklist"]]
    if shots:
        lines += ["", "## Screens", ""] + [f"- [{s['route']} · {s['width']} px{' · dark' if s['dark'] else ''}](screens/{s['file']})" for s in shots]
    if result.get("limits"):
        lines += ["", "## Limits", "", result["limits"]]
    return "\n".join(lines) + "\n"


def fix_job_text(findings: list[dict[str, Any]], min_severity: int = 2) -> str:
    """A job description that asks for the worthwhile findings to be fixed, most severe first."""
    picked = [f for f in findings if f["severity"] >= min_severity] or findings
    return "Fix these UX and design findings:\n" + "\n".join(
        f"- [{SEVERITY[f['severity']]}] {f['screen'] or 'All screens'}: {f['problem']}" + (f" Fix: {f['fix']}" if f["fix"] else "")
        for f in picked[:20])


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
