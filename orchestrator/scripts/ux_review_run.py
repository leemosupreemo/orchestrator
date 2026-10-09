#!/usr/bin/env python3
"""Run a UX and design review (see orchestrator/ux_review.py).

    ux_review_run.py pass [--no-screens]          the whole product: a UX pass and a design pass, merged
    ux_review_run.py job <job.json>               one change: only if its diff touches the interface

Results land in .orchestrator/output/ux-pass/<when>/ or .orchestrator/output/<job>/ux-review/ as report.md,
result.json and screens/, and a job records a summary under `ux_review`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import OUTPUT_DIR, PROMPTS_DIR, ROOT, now_iso, read_json, write_json, write_text  # noqa: E402
from model_router import ModelRole  # noqa: E402

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


def capture(cfg: dict, screens_dir: Path, *, dark: bool | None, job_id: str = "") -> tuple[list[dict], list[str]]:
    limits = []
    shots: list[dict] = []
    if cfg["url"]:
        shots, problem = ux.capture_web(ROOT, cfg, screens_dir, dark=dark)
        if problem:
            limits.append(problem)
        else:
            limits.append("Screens show the app currently served at the configured URL. Baseline routes show first load; "
                          "declared screen states include their capture interactions. Other states may be missing.")
    if cfg["simulator"]:
        result = subprocess.run([sys.executable, str(SCRIPTS_DIR / "simulator_visual_check.py"), "--job", job_id or "ux-pass"],
                                cwd=ROOT, capture_output=True, text=True, check=False)
        latest = OUTPUT_DIR / "manual" / "latest"
        pngs = sorted(latest.glob("screenshot_*.png")) if result.returncode == 0 and latest.exists() else []
        screens_dir.mkdir(parents=True, exist_ok=True)
        for n, png in enumerate(pngs, start=1):
            shutil.copy2(png, screens_dir / f"simulator-{n}.png")
            shots.append({"file": f"simulator-{n}.png", "route": "iOS simulator, after launch", "width": 0, "dark": False})
        if not pngs:
            limits.append("The iOS simulator check didn't produce screenshots; see its log under output/manual/.")
    if not cfg["url"] and not cfg["simulator"]:
        limits.append("No screens are configured (ui_review in .orchestrator/project.json), so this review works "
                      "from the code alone and can't judge layout, spacing or contrast as rendered.")
    return shots, limits


def review(prompt: str, shots: list[dict], screens_dir: Path, *, model: str, allowed_models: list | None) -> tuple[dict, str, str]:
    from llm import run_llm
    images = [screens_dir / s["file"] for s in shots]
    raw, used, _ = run_llm(model, prompt, cwd=ROOT, timeout=900, role=ModelRole.REVIEWER,
                           allowed_models=allowed_models, images=images or None)
    return ux.parse_result(raw), raw, used


def write_outputs(out_dir: Path, result: dict, raw: str, *, title: str, scope: str, shots: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "result.json", {**result, "screens": shots, "title": title, "scope": scope, "at": datetime.now().astimezone().isoformat()})
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
    stage = "Capture screens" if not no_screens and (cfg["url"] or cfg["simulator"]) else "Prepare interface code"
    print(f"[1/4] {stage}", flush=True)
    shots, limits = ([], ["Screens were skipped for this run."]) if no_screens else capture(cfg, screens_dir, dark=None)
    save_screens(out_dir, shots, limits, title="Whole-product review")
    tracked = git_out("ls-files").splitlines()
    interface = ux.frontend_files(tracked, cfg["paths"])
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
    write_outputs(out_dir, result, "\n\n".join(raws), title="UX and design pass", scope="Whole product", shots=shots)
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
    try:
        shots, limits = capture(cfg, pending_dir, dark=None, job_id=job["job_id"])
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
