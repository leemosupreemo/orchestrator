#!/usr/bin/env python3
from __future__ import annotations

import argparse
import plistlib
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import (
    OUTPUT_DIR,
    ROOT,
    extract_commands,
    get_best_simulator_destination,
    command_with_destination,
    print_divider,
    print_header,
    timestamp,
    write_text,
)
from manual_run import cleanup_logs, stream_command
from orchestrator.project_config import PROJECT_CONFIG


DEFAULT_APP_PATH = (
    Path(PROJECT_CONFIG.visual_app_path)
    if PROJECT_CONFIG.visual_app_path
    else Path(PROJECT_CONFIG.derived_data_path) / "Build" / "Products" / "Debug-iphonesimulator" / f"{PROJECT_CONFIG.scheme}.app"
)
DEFAULT_BUNDLE_ID = PROJECT_CONFIG.app_bundle_id or ""


def simulator_udid(destination: str) -> str | None:
    match = re.search(r"(?:^|,)id=([^,]+)", destination)
    return match.group(1) if match else None


def run_simctl(args: list[str], log_file: Path) -> bool:
    cmd = ["xcrun", "simctl", *args]
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"$ {shlex.join(cmd)}\n")
        result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        if result.stdout:
            print(result.stdout, end="")
            log.write(result.stdout)
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
            log.write(result.stderr)
        log.write(f"exit={result.returncode}\n\n")
    if result.returncode == 0:
        return True
    return args[:1] == ["boot"] and "Unable to boot device in current state: Booted" in result.stderr


def markdown_link(label: str, path: Path) -> str:
    rel_path = path.relative_to(ROOT)
    abs_path = path.resolve()
    abs_uri = f"file://{abs_path}"
    link_text = f"[{label}]({rel_path})"
    
    if sys.stdout.isatty():
        interactive_link = f"\033]8;;{abs_uri}\033\\{link_text}\033]8;;\033\\"
    else:
        interactive_link = link_text
        
    return f"{interactive_link} ({abs_uri})"


def screenshot_report_block(idx: int, path: Path) -> str:
    return f"### Screenshot {idx}\n[![Screenshot {idx}]({path.name})]({path.name})\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build, launch, and screenshot the iOS simulator for visual UI review.")
    parser.add_argument("--no-build", action="store_true", help="Skip xcodebuild and reuse the existing built app.")
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="Bundle identifier to launch.")
    parser.add_argument("--app-path", default=str(DEFAULT_APP_PATH), help="Built .app path to install.")
    parser.add_argument("--wait", type=float, default=3.0, help="Seconds to wait after launch before the first screenshot.")
    parser.add_argument("--screenshots", type=int, default=1, help="Number of screenshots to capture.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between screenshots.")
    args = parser.parse_args()
    bundle_id = args.bundle_id

    manual_base = OUTPUT_DIR / "manual"
    out_dir = manual_base / f"{timestamp()}-visual-check"
    out_dir.mkdir(parents=True, exist_ok=True)

    latest_link = manual_base / "latest"
    if latest_link.exists() or latest_link.is_symlink():
        latest_link.unlink()
    try:
        latest_link.symlink_to(out_dir.name, target_is_directory=True)
    except Exception as exc:
        print(f"Warning: Could not update latest symlink: {exc}")

    destination = get_best_simulator_destination()
    udid = simulator_udid(destination)
    if not udid:
        print(f"❌ Could not determine simulator UDID from destination: {destination}")
        sys.exit(1)

    run_log = out_dir / "visual_check.log"
    build_ok = True

    print(f"Simulator destination: {destination}")
    print(f"Output: {out_dir.relative_to(ROOT)}")

    if not args.no_build:
        build_cmd, _ = extract_commands()
        build_cmd = command_with_destination(build_cmd, destination)
        build_ok = stream_command(build_cmd, out_dir / "build.log")
        if not build_ok:
            print("\n❌ Build failed; skipping simulator launch.")
            sys.exit(1)

    app_path = Path(args.app_path)
    if not app_path.exists():
        print(f"❌ Built app not found: {app_path}")
        sys.exit(1)

    # Automatically resolve bundle_id from Info.plist if not specified.
    if not bundle_id:
        print("🔍 Attempting to automatically extract bundle identifier from Info.plist...")
        plist_path = app_path / "Info.plist"
        if plist_path.exists():
            try:
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                    bundle_id = plist.get("CFBundleIdentifier")
            except Exception as exc:
                print(f"Warning: Could not parse Info.plist: {exc}")

        if not bundle_id:
            print(
                "❌ Bundle id is required. Set app_bundle_id in project.json, "
                "pass --bundle-id, or ensure the built app's Info.plist contains CFBundleIdentifier."
            )
            sys.exit(1)
        print(f"✅ Automatically resolved Bundle ID: {bundle_id}")

    steps = [
        ["boot", udid],
        ["bootstatus", udid, "-b"],
        ["install", udid, str(app_path)],
        ["launch", udid, bundle_id],
    ]
    for step in steps:
        if not run_simctl(step, run_log):
            print(f"❌ simctl step failed: {' '.join(step)}")
            sys.exit(1)

    time.sleep(max(0, args.wait))

    screenshot_paths: list[Path] = []
    for idx in range(max(1, args.screenshots)):
        screenshot_path = out_dir / f"screenshot_{idx + 1}.png"
        if not run_simctl(["io", udid, "screenshot", str(screenshot_path)], run_log):
            print("❌ Screenshot capture failed.")
            sys.exit(1)
        screenshot_paths.append(screenshot_path)
        if idx < args.screenshots - 1:
            time.sleep(max(0, args.interval))

    report_lines = [
        "# 📱 Simulator Visual Check Report",
        "",
        "## Overview",
        f"- **Captured Scope:** Active foreground root view & initial UI state of `{bundle_id}` after launch.",
        f"- **Screenshots Taken:** {len(screenshot_paths)} screenshot(s) (with {args.wait:.1f}s post-launch settle delay).",
        f"- **Target Simulator:** `{destination}`",
        f"- **Bundle Identifier:** `{bundle_id}`",
        f"- **App Binary:** `{app_path}`",
        f"- **Build Status:** `{'Skipped (reused binary)' if args.no_build else 'Passed'}`",
        f"- **Output Folder:** [{out_dir.name}](file://{out_dir.resolve()})",
        "",
        "## Screenshots",
        "",
    ]
    report_lines.extend(
        screenshot_report_block(idx, path)
        for idx, path in enumerate(screenshot_paths, start=1)
    )
    report_lines.extend([
        "",
        "## Visual QA Checklist",
        "- [ ] Verify root view layout and element alignment across safe area insets.",
        "- [ ] Check for text clipping, missing labels, or truncated strings.",
        "- [ ] Ensure light/dark color contrast and asset rendering look correct.",
        "- [ ] Confirm that no unhandled blank, error, or loading freeze states occurred on startup.",
        "",
    ])
    write_text(out_dir / "report.md", "\n".join(report_lines))

    print_header("📱 Simulator Visual Check Complete")
    print(f"   \033[1;36m• Target App:\033[0m        \033[1;97m{bundle_id}\033[0m \033[90m({app_path.name})\033[0m")
    print(f"   \033[1;36m• Scope Captured:\033[0m    \033[97mForeground root view & initial UI state after launch\033[0m")
    print(f"   \033[1;36m• Screenshots:\033[0m       \033[1;93m{len(screenshot_paths)} screenshot(s)\033[0m \033[90m({args.wait:.1f}s post-launch settle delay)\033[0m")
    print(f"   \033[1;36m• Target Device:\033[0m     \033[97m{destination}\033[0m")
    print(f"   \033[1;36m• Output Folder:\033[0m     {markdown_link(str(out_dir.relative_to(ROOT)), out_dir)}")
    print(f"   \033[1;36m• Visual Report:\033[0m     {markdown_link('Open report.md', out_dir / 'report.md')}")
    if screenshot_paths:
        print(f"   \033[1;36m• Captured Images:\033[0m")
        for idx, path in enumerate(screenshot_paths, start=1):
            print(f"     \033[90m- Screenshot #{idx}:\033[0m {markdown_link(path.name, path)}")
    print_divider()
    cleanup_logs(manual_base)


if __name__ == "__main__":
    main()
