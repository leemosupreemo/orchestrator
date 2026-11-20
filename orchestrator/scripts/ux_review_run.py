#!/usr/bin/env python3
"""Run a UX and design review (see orchestrator/ux_review.py).

    ux_review_run.py pass [--no-screens]          the whole product: a UX pass and a design pass, merged
    ux_review_run.py job <job.json>               one change: only if its diff touches the interface

Results land in .orchestrator/output/ux-pass/<when>/ or .orchestrator/output/<job>/ux-review/ as report.md,
result.json and screens/, and a job records a summary under `ux_review`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import OUTPUT_DIR, PROMPTS_DIR, ROOT, now_iso, read_json, write_json, write_text  # noqa: E402
from model_router import ModelRole  # noqa: E402

from orchestrator import ux_native as native  # noqa: E402
from orchestrator import ux_review as ux  # noqa: E402
from orchestrator.project_config import PROJECT_CONFIG  # noqa: E402

AREA_FOCUS = {
    "ux": "This run covers the UX half of the checklist only: mark every design.* item n/a and report only UX findings.",
    "design": "This run covers the Design half of the checklist only: mark every ux.* item n/a and report only design findings.",
}


def git_out(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout


def project_settings() -> dict:
    project_file = ROOT / ".orchestrator" / "project.json"
    return ux.settings(read_json(project_file) if project_file.exists() else {})


PREVIEW_START_TIMEOUT = 45.0


def _project_json_data() -> dict:
    for candidate in (OUTPUT_DIR.parent / "project.json", ROOT / ".orchestrator" / "project.json"):
        if candidate.is_file():
            data = read_json(candidate)
            if isinstance(data, dict):
                return data
    return {}


def preview_web_target(cfg: dict, *, branch: str = "") -> tuple[dict, str]:
    """When ui_review.url isn't set, reuse a branch-matched running Live preview or preview settings from project.json."""
    if cfg.get("url"):
        return cfg, ""
    dev_dir = OUTPUT_DIR / "dev-previews"
    previews: list[dict] = []
    if dev_dir.is_dir():
        for path in sorted(dev_dir.glob("*.json")):
            if path.name.endswith(".remote.json"):
                continue
            data = read_json(path)
            if not isinstance(data, dict):
                continue
            try:
                os.kill(int(data.get("pid") or 0), 0)
            except (OSError, ValueError, TypeError):
                continue
            target_url = str(data.get("direct") or data.get("url") or "").strip()
            if target_url:
                previews.append({"branch": str(data.get("branch") or ""), "url": target_url})
    picked = next((p for p in previews if p["branch"] == branch), None) if branch else (previews[0] if previews else None)
    if picked:
        return {**cfg, "url": picked["url"]}, "live preview"
    proj = _project_json_data()
    if proj:
        p_url = str(proj.get("preview_url") or "").strip()
        p_cmd = str(proj.get("preview_dev_command") or "").strip()
        if p_url:
            return {**cfg, "url": p_url, "start_command": p_cmd or cfg.get("start_command", "")}, "preview settings"
        if p_cmd:
            files = git_out("ls-files").splitlines()
            inferred = ux.inferred_targets(ROOT, files, config=proj).get("web") or {}
            if inferred.get("url"):
                return {**cfg, "url": str(inferred["url"]), "start_command": p_cmd}, "preview command"
    return cfg, ""


def _resolve_preview_branch(branch: str = "") -> str:
    target = (branch or git_out("branch", "--show-current")).strip()
    if not target:
        return ""
    branches = {b.strip() for b in git_out("branch", "--format=%(refname:short)").splitlines() if b.strip()}
    return target if target in branches else ""


def can_auto_preview(cfg: dict, *, branch: str = "") -> bool:
    """True when we can spin up a temporary branch-matched Live preview for screenshot capture."""
    if cfg.get("url") or not _resolve_preview_branch(branch) or not ux.find_browser():
        return False
    from orchestrator.scripts import dev_preview
    try:
        dev_preview.dev_commands(ROOT, _project_json_data())
        return True
    except ValueError:
        return False


@contextlib.contextmanager
def temporary_preview(branch: str, *, timeout: float = PREVIEW_START_TIMEOUT):
    """Spin up a temporary dev_preview for `branch`, yield `(url, problem)`, and always tear it down cleanly."""
    from orchestrator.scripts import dev_preview

    slug = dev_preview.channel_name(branch)
    state_file = OUTPUT_DIR / "dev-previews" / f"{slug}.json"
    env = {**os.environ, "ORCHESTRATOR_RUNTIME_DIR": str(OUTPUT_DIR.parent)}
    proc = subprocess.Popen(
        [sys.executable, str(SCRIPTS_DIR / "dev_preview.py"), "--branch", branch, "--root", str(ROOT)],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )
    url = ""
    problem = ""
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if state_file.is_file():
                try:
                    data = json.loads(state_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    data = {}
                if isinstance(data, dict) and data.get("pid") == proc.pid:
                    candidate = str(data.get("direct") or data.get("url") or "").strip()
                    if candidate:
                        url = candidate
                        break
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        if not url:
            if proc.poll() is not None:
                out = (proc.stdout.read() if proc.stdout else "").strip()
                last = next((ln.strip() for ln in reversed(out.splitlines()) if ln.strip()), "")
                problem = f"Temporary live preview for {branch} stopped before it was ready" + (f": {last}" if last else ".")
            else:
                problem = f"Temporary live preview for {branch} didn't become ready within {int(timeout)}s."
        yield url, problem
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=5)
            except OSError:
                pass
        if proc.stdout:
            proc.stdout.close()
        try:
            if state_file.is_file() and json.loads(state_file.read_text(encoding="utf-8")).get("pid") == proc.pid:
                state_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def with_detected_devices(cfg: dict, raw: dict) -> tuple[dict, str]:
    """When ui_review names no native target, use the simulator or device setup the project already has, so a job's UI
    change still gets screenshots. A target set to false in ui_review stays off. Returns (cfg, note); note is "" if unchanged."""
    if cfg["simulator"] or cfg["android"] or cfg["macos"] or cfg.get("capture"):
        return cfg, ""
    ui = raw.get("ui_review") if isinstance(raw.get("ui_review"), dict) else {}
    found = ux.inferred_targets(ROOT, git_out("ls-files").splitlines(), config=raw)
    used = []
    if ui.get("simulator") is not False and "ios" in found and PROJECT_CONFIG.uses_xcode and shutil.which("xcrun"):
        cfg = {**cfg, "simulator": True}
        used.append("iOS simulator")
    android = found.get("android") or {}
    if ui.get("android") is not False and android.get("package") and native.find_adb():
        cfg = {**cfg, "android": {"package": android["package"], "apk": "", "avd": "", "build_command": android.get("build_command", ""), "screens": []}}
        used.append("Android device")
    if not used:
        return cfg, ""
    return cfg, (f"Screens came from the project's {' and '.join(used)} setup, at launch only. "
                 "Add ui_review to project.json to choose screens, or set a target to false to skip it.")


def capture(cfg: dict, screens_dir: Path, *, dark: bool | None, job_id: str = "", branch: str = "") -> tuple[list[dict], list[str]]:
    limits = []
    shots: list[dict] = []
    web_cfg, preview_source = preview_web_target(cfg, branch=branch)
    auto_preview_attempted = False
    if web_cfg["url"]:
        shots, problem = ux.capture_web(ROOT, web_cfg, screens_dir, dark=dark)
        if problem:
            limits.append(problem)
        elif preview_source:
            limits.append(f"Screens were captured from the project's {preview_source} ({web_cfg['url']}).")
        else:
            limits.append("Screens show the app currently served at the configured URL. Baseline routes show first load; "
                          "declared screen states include their capture interactions. Other states may be missing.")
    elif can_auto_preview(cfg, branch=branch):
        auto_preview_attempted = True
        target_branch = _resolve_preview_branch(branch)
        with temporary_preview(target_branch) as (temp_url, preview_problem):
            if temp_url:
                shots, problem = ux.capture_web(ROOT, {**cfg, "url": temp_url}, screens_dir, dark=dark)
                if problem:
                    limits.append(problem)
                else:
                    limits.append(f"Screens were captured from a temporary live preview of {target_branch} ({temp_url}).")
            elif preview_problem:
                limits.append(preview_problem)
    if cfg["simulator"]:
        links = [arg for link in cfg["simulator_screens"] for arg in ("--screen", link)]
        result = subprocess.run([sys.executable, str(SCRIPTS_DIR / "simulator_visual_check.py"), "--job", job_id or "ux-pass", *links],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        latest = OUTPUT_DIR / "manual" / "latest"
        pngs = sorted(latest.glob("screenshot_*.png")) if result.returncode == 0 and latest.exists() else []
        screens_dir.mkdir(parents=True, exist_ok=True)
        labels = read_json(latest / "screens.json") if (latest / "screens.json").is_file() else {}
        for n, png in enumerate(pngs, start=1):
            shutil.copy2(png, screens_dir / f"simulator-{n}.png")
            shots.append({"file": f"simulator-{n}.png", "route": labels.get(png.name, "iOS simulator, after launch"), "width": 0,
                          "dark": png.stem.endswith("_dark")})
        if not pngs:
            limits.append("The iOS simulator check didn't produce screenshots; see its log under output/manual/.")
    if cfg["android"]:
        android_shots, problem = native.capture_android(ROOT, cfg["android"], screens_dir, dark_mode=cfg["dark_mode"] if dark is None else dark,
                                                        log=lambda line: print(line, flush=True))
        shots += android_shots
        if problem:
            limits.append(problem)
    if cfg["macos"]:
        mac_shots, problem = native.capture_macos(ROOT, cfg["macos"], screens_dir, log=lambda line: print(line, flush=True))
        shots += mac_shots
        if problem:
            limits.append(problem)
    if cfg.get("capture"):
        own_shots, problem = native.capture_command(ROOT, cfg["capture"], screens_dir, job_id=job_id,
                                                    dark_mode=cfg["dark_mode"] if dark is None else dark, log=lambda line: print(line, flush=True))
        shots += own_shots
        if problem:
            limits.append(problem)
    if not web_cfg["url"] and not auto_preview_attempted and not ux.has_screens(cfg):
        limits.append("No screens configured (ui_review in project.json), so layout, spacing and contrast weren't judged.")
    return shots, limits


def review(prompt: str, shots: list[dict], screens_dir: Path, *, model: str, allowed_models: list | None) -> tuple[dict, str, str]:
    from llm import run_llm
    images = [screens_dir / s["file"] for s in shots]
    raw, used, _ = run_llm(model, prompt, cwd=ROOT, timeout=900, role=ModelRole.REVIEWER,
                           allowed_models=allowed_models, images=images or None)
    return ux.parse_result(raw), raw, used


def write_outputs(out_dir: Path, result: dict, raw: str, *, title: str, scope: str, shots: list[dict], **extra: object) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "result.json", {**result, **extra, "screens": shots, "title": title, "scope": scope, "at": datetime.now().astimezone().isoformat()})
    write_text(out_dir / "raw.md", raw)
    write_text(out_dir / "report.md", ux.report_markdown(result, title=title, scope=scope, shots=shots, when=now_iso()[:16].replace("T", " ")))


def save_screens(out_dir: Path, shots: list[dict], limits: list[str], **context: object) -> dict:
    """Screenshot evidence survives independently of the model's review result."""
    evidence = {"at": datetime.now().astimezone().isoformat(), "screens": shots, "limits": limits, **context}
    write_json(out_dir / "screens.json", evidence)
    return evidence


def product_pass(no_screens: bool, model: str) -> int:
    cfg = project_settings()
    out_dir = OUTPUT_DIR / "ux-pass" / ux.stamp()
    screens_dir = out_dir / "screens"
    current_branch = git_out("branch", "--show-current").strip()
    preview_cfg, _ = preview_web_target(cfg, branch=current_branch)
    stage = "Capture screens" if not no_screens and (ux.has_screens(cfg) or preview_cfg["url"] or can_auto_preview(cfg, branch=current_branch)) else "Prepare interface code"
    print(f"[1/4] {stage}", flush=True)
    shots, limits = ([], ["Screens were skipped for this run."]) if no_screens else capture(cfg, screens_dir, dark=None, branch=current_branch)
    save_screens(out_dir, shots, limits, title="Whole-product review")
    tracked = git_out("ls-files").splitlines()
    interface = ux.frontend_files(tracked, cfg["paths"])
    coverage = "empty" if not interface and not shots else "full" if shots else "code_only"
    if coverage == "empty":  # nothing to look at: don't spend model calls on it
        note = "This project has no interface files or screens yet, so there is nothing to review."
        result = {"summary": note, "checklist": [], "findings": [], "limits": " ".join(limits)}
        print("[4/4] No interface to review", flush=True)
        write_outputs(out_dir, result, "", title="UX and design pass", scope="Whole product", shots=shots, coverage=coverage, interface_files=0)
        print(f"[notice] {note} Add some UI, then run the review again.", flush=True)
        print(f"\nNothing to review. Report: {(out_dir / 'report.md').relative_to(ROOT)}")
        return 0
    from orchestrator import prd
    checklist = (PROMPTS_DIR / "ux_reviewer.md").read_text(encoding="utf-8")
    merged = {"summary": [], "checklist": {}, "findings": [], "limits": set(limits)}
    raws = []
    for area in ("ux", "design"):  # two focused passes, then one report
        print(f"[{2 if area == 'ux' else 3}/4] {'Check usability' if area == 'ux' else 'Check visual design'}", flush=True)
        prompt = ux.build_prompt(
            checklist, scope=f"A whole-product pass. {AREA_FOCUS[area]}",
            ask=prd.context_block(ROOT, "reviewer") or "", conventions=ux.conventions_text(ROOT, cfg["conventions"]),
            shots=shots, shot_dir=screens_dir,
            code="Interface files in this repository (read the ones you need):\n" + "\n".join(f"- {f}" for f in interface[:400]),
            limits=limits)
        result, raw, used = review(prompt, shots, screens_dir, model=model, allowed_models=None)
        raws.append(f"# {area} pass ({used})\n\n{raw}")
        if result["summary"]:
            merged["summary"].append(result["summary"])
        merged["checklist"].update({c["id"]: c for c in result["checklist"] if c["id"].startswith(area)})
        merged["findings"] += [f for f in result["findings"] if f["area"] == area]
        if result["limits"]:
            merged["limits"].add(result["limits"])
    result = {"summary": " ".join(merged["summary"]),
              "checklist": [merged["checklist"][i] for ids in ux.CHECKLIST.values() for i in ids],
              "findings": sorted(merged["findings"], key=lambda f: -f["severity"]),
              "limits": " ".join(sorted(merged["limits"]))}
    print("[4/4] Save report", flush=True)
    write_outputs(out_dir, result, "\n\n".join(raws), title="UX and design pass", scope="Whole product", shots=shots,
                  coverage=coverage, interface_files=len(interface))
    if coverage == "code_only":
        print("[notice] No screenshots were captured, so only the interface code was reviewed. Layout, spacing and contrast were not judged.", flush=True)
    c = ux.counts(result)
    print(f"\n{c['findings']} findings ({c['major']} major or worse). Report: {(out_dir / 'report.md').relative_to(ROOT)}")
    return 0


def job_review(job_path: Path) -> int:
    job = read_json(job_path)
    cfg = project_settings()
    record = {"at": now_iso()}
    branch = job.get("branch")
    base = job.get("base_branch") or PROJECT_CONFIG.base_branch
    changed = git_out("diff", "--name-only", f"{base}...{branch}").splitlines() if branch else []
    interface = ux.frontend_files(changed, cfg["paths"])
    manifest_path = OUTPUT_DIR / job["job_id"] / "ux-screens.json"
    if not interface and not manifest_path.is_file():
        record["skipped"] = "No interface files changed."
    if "skipped" in record:
        print(f"UX and design check: {record['skipped']}")
        job = read_json(job_path)
        job["ux_review"] = record
        write_json(job_path, job)
        return 0

    out_dir = OUTPUT_DIR / job["job_id"] / "ux-review"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("result.json", "raw.md", "report.md"):
        (out_dir / name).unlink(missing_ok=True)  # old findings must not describe a new capture
    capture_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    pending_dir = out_dir / f".capture-{capture_id}"
    screens_dir = out_dir / "screens"
    plan_limits = []
    if manifest_path.is_file():
        try:
            manifest = read_json(manifest_path)
            cfg = ux.capture_plan(cfg, manifest)
            plan_limits.extend(str(note)[:300] for note in manifest.get("notes", []) if isinstance(note, str))
        except (ux.ConfigError, ValueError, TypeError) as exc:
            plan_limits.append(f"Capture plan could not be read: {exc}. Only configured pages were captured.")
    else:
        plan_limits.append("No capture plan was provided for this UI change. Only configured pages were captured; new pages and interactive states may be missing.")
    project_file = ROOT / ".orchestrator" / "project.json"
    raw_project = read_json(project_file) if project_file.exists() else {}
    cfg, device_note = with_detected_devices(cfg, raw_project if isinstance(raw_project, dict) else {})
    if device_note:
        plan_limits.append(device_note)
    try:
        shots, limits = capture(cfg, pending_dir, dark=None, job_id=job["job_id"], branch=branch or "")
    except (OSError, subprocess.SubprocessError, ux.BrowserError) as exc:
        shots, limits = [], [f"Screenshot capture failed: {exc}"]
    screens_dir.mkdir(parents=True, exist_ok=True)
    for shot in shots:
        source = pending_dir / shot["file"]
        name = f"{capture_id}-{shot['file']}"
        shutil.move(str(source), screens_dir / name)
        shot["file"] = name
    if pending_dir.exists():
        shutil.rmtree(pending_dir)
    limits = plan_limits + limits
    evidence = save_screens(out_dir, shots, limits, capture_id=capture_id, title=job.get("title", job["job_id"]), branch=branch, files=interface)
    write_json(out_dir / "captures" / f"{capture_id}.json", evidence)
    record.update({"at": evidence["at"], "files": interface, "screens": len(shots), "screen_list": shots,
                   "capture_status": "captured" if shots else "missing", "capture_limits": limits})
    current = read_json(job_path)
    current["ux_review"] = record
    write_json(job_path, current)
    if not cfg["review_changes"]:
        current["ux_review"]["skipped"] = "AI review is turned off. Screenshot evidence was still captured."
        write_json(job_path, current)
        return 0
    diff = git_out("diff", f"{base}...{branch}", "--", *interface)
    if len(diff) > ux.MAX_DIFF_CHARS:
        diff = diff[:ux.MAX_DIFF_CHARS] + "\n…[diff truncated]"
    brief_file = OUTPUT_DIR / job["job_id"] / "brief.md"
    checklist = (PROMPTS_DIR / "ux_reviewer.md").read_text(encoding="utf-8")
    prompt = ux.build_prompt(
        checklist, scope=f"One change: the job \"{job.get('title', '')}\". Review only what it touches or makes worse.",
        ask=brief_file.read_text(encoding="utf-8")[:8000] if brief_file.exists() else job.get("title", ""),
        conventions=ux.conventions_text(ROOT, cfg["conventions"]), shots=shots, shot_dir=screens_dir,
        code=f"Interface changes ({len(interface)} files):\n```diff\n{diff}\n```", limits=limits)
    print(f"UX and design check on {len(interface)} interface files, {len(shots)} screens…")
    try:
        result, raw, used = review(prompt, shots, screens_dir, model=job.get("reviewer") or "gemini",
                                   allowed_models=job.get("allowed_models"))
    except Exception as exc:  # a review that can't run never fails the job
        print(f"UX and design check couldn't run: {exc}")
        job = read_json(job_path)
        job["ux_review"] = {**record, "error": str(exc)[:300]}
        write_json(job_path, job)
        return 0
    write_outputs(out_dir, result, raw, title=f"UX and design check: {job.get('title', '')}", scope="One change", shots=shots)
    job = read_json(job_path)  # the worker may have written meanwhile
    job["ux_review"] = {**record, "model": used, "counts": ux.counts(result), "summary": result["summary"],
                        "files": interface, "screens": len(shots)}
    write_json(job_path, job)
    c = ux.counts(result)
    print(f"UX and design check: {c['findings']} findings ({c['major']} major or worse).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="scope", required=True)
    p = sub.add_parser("pass", help="Review the whole product")
    p.add_argument("--no-screens", action="store_true", help="Review from the code only")
    p.add_argument("--model", default="gemini")
    j = sub.add_parser("job", help="Review one job's interface changes")
    j.add_argument("job_file")
    args = parser.parse_args(argv)
    try:
        if args.scope == "pass":
            return product_pass(args.no_screens, args.model)
        return job_review(Path(args.job_file))
    except ux.ConfigError as exc:
        print(f"ui_review in .orchestrator/project.json: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
