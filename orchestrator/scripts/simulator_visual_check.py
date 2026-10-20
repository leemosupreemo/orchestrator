#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import plistlib
import re
import shlex
import signal
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
    mode_tag = " (Light Mode)" if "light" in path.stem else (" (Dark Mode)" if "dark" in path.stem else "")
    return f"### Screenshot {idx}{mode_tag}\n[![Screenshot {idx}{mode_tag}]({path.name})]({path.name})\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build, launch, and screenshot the iOS simulator for visual UI review.")
    parser.add_argument("--no-build", action="store_true", help="Skip xcodebuild and reuse the existing built app.")
    parser.add_argument("--job", default="", help="Job id this check belongs to (shown on that job's page).")
    parser.add_argument("--bundle-id", default=DEFAULT_BUNDLE_ID, help="Bundle identifier to launch.")
    parser.add_argument("--app-path", default=str(DEFAULT_APP_PATH), help="Built .app path to install.")
    parser.add_argument("--wait", type=float, default=3.0, help="Seconds to wait after launch before the first screenshot.")
    parser.add_argument("--screenshots", type=int, default=1, help="Number of screenshots to capture per appearance mode.")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between screenshots.")
    parser.add_argument("--appearance", choices=["both", "light", "dark", "current"], default="both", help="Simulator appearance: both, light, dark, or current (default: both).")
    parser.add_argument("--mask", choices=["alpha", "black", "ignored"], default="alpha", help="Screenshot device corner mask (default: alpha).")
    parser.add_argument("--status-bar", choices=["override", "none"], default="override", help="Set clean 9:41 status bar override during check (default: override).")
    parser.add_argument("--video", dest="video", action="store_true", default=True, help="Record an MP4 video of the app launch and settle (default: enabled).")
    parser.add_argument("--no-video", dest="video", action="store_false", help="Disable launch video recording.")
    parser.add_argument("--screen", action="append", default=[], help="Link that opens another screen in the app (myapp://settings); captured after launch. Repeatable.")
    parser.add_argument("--video-codec", choices=["h264", "hevc"], default="h264", help="Video codec for browser HTML5 compatibility (default: h264).")
    args = parser.parse_args()
    bundle_id = args.bundle_id

    manual_base = OUTPUT_DIR / "manual"
    out_dir = manual_base / f"{timestamp()}-visual-check"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.job:
        write_text(out_dir / "meta.json", json.dumps({"job_id": args.job}))

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

    if not getattr(args, "no_build", False):
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
    ]
    for step in steps:
        if not run_simctl(step, run_log):
            print(f"❌ simctl step failed: {' '.join(step)}")
            sys.exit(1)

    # Clean status bar override for marketing/QA quality screenshots
    status_bar_mode = getattr(args, "status_bar", "override")
    if status_bar_mode == "override":
        run_simctl([
            "status_bar", udid, "override",
            "--time", "9:41",
            "--batteryState", "charged",
            "--batteryLevel", "100",
            "--cellularBars", "4",
            "--wifiBars", "3",
        ], run_log)

    # Launch video recording in background
    video_proc: subprocess.Popen[str] | None = None
    video_path = out_dir / "launch.mp4"
    should_record_video = bool(getattr(args, "video", False))
    video_codec = getattr(args, "video_codec", "h264")
    if should_record_video:
        video_cmd = ["xcrun", "simctl", "io", udid, "recordVideo", f"--codec={video_codec}", "--force", str(video_path)]
        with open(run_log, "a", encoding="utf-8") as log:
            log.write(f"$ {shlex.join(video_cmd)}\n")
        try:
            video_proc = subprocess.Popen(
                video_cmd,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.5)
        except Exception as exc:
            print(f"Warning: Could not start video recording: {exc}")
            video_proc = None

    try:
        if not run_simctl(["launch", udid, bundle_id], run_log):
            print(f"❌ simctl step failed: launch {bundle_id}")
            sys.exit(1)

        wait_seconds = max(0, getattr(args, "wait", 3.0))
        time.sleep(wait_seconds)

        # Appearance mode setup
        appearance_choice = getattr(args, "appearance", "both")
        if appearance_choice == "both":
            modes: list[str | None] = ["light", "dark"]
        elif appearance_choice in ("light", "dark"):
            modes = [appearance_choice]
        else:
            modes = [None]

        mask_policy = getattr(args, "mask", "alpha")
        screenshot_paths: list[Path] = []
        screen_labels: dict[str, str] = {}  # file -> the link that opened it, for the UX review
        shot_count = max(1, getattr(args, "screenshots", 1))

        for mode in modes:
            if mode:
                run_simctl(["ui", udid, "appearance", mode], run_log)
                time.sleep(0.4)

            for idx in range(shot_count):
                suffix = f"_{mode}" if mode else ""
                idx_str = f"_{idx + 1}" if shot_count > 1 or not mode else (f"_{idx + 1}" if len(modes) > 1 else f"_{idx + 1}")
                screenshot_path = out_dir / f"screenshot{idx_str}{suffix}.png"

                shot_cmd = ["io", udid, "screenshot"]
                if mask_policy:
                    shot_cmd.append(f"--mask={mask_policy}")
                shot_cmd.append(str(screenshot_path))

                if not run_simctl(shot_cmd, run_log):
                    # Fallback to unmasked screenshot if --mask flag is rejected
                    if not run_simctl(["io", udid, "screenshot", str(screenshot_path)], run_log):
                        print("❌ Screenshot capture failed.")
                        sys.exit(1)

                screenshot_paths.append(screenshot_path)
                if idx < shot_count - 1:
                    time.sleep(max(0, getattr(args, "interval", 1.0)))

            for link in args.screen:
                if not run_simctl(["openurl", udid, link], run_log):
                    print(f"⚠️ Couldn't open {link} in the simulator.")
                    continue
                time.sleep(max(1.5, getattr(args, "interval", 1.0)))
                link_path = out_dir / f"screenshot_{re.sub(r'[^a-z0-9]+', '-', link.lower()).strip('-')[:40]}{'_' + mode if mode else ''}.png"
                if run_simctl(["io", udid, "screenshot", str(link_path)], run_log):
                    screenshot_paths.append(link_path)
                    screen_labels[link_path.name] = link

    finally:
        # Finalize video recording
        video_recorded = False
        if video_proc:
            try:
                video_proc.send_signal(signal.SIGINT)
                video_proc.wait(timeout=6.0)
            except Exception:
                try:
                    video_proc.kill()
                except Exception:
                    pass
            if video_path.is_file() and video_path.stat().st_size > 1024:
                video_recorded = True

        # Clear status bar override to return simulator to host status
        if status_bar_mode == "override":
            run_simctl(["status_bar", udid, "clear"], run_log)

    if screen_labels:
        write_text(out_dir / "screens.json", json.dumps(screen_labels))
    report_lines = [
        "# 📱 Simulator Visual Check Report",
        "",
        "> Boots the app on the simulator to check launch layout, element alignment, and safe-area insets. Click any screen to zoom and inspect.",
        "",
        "## Overview",
        f"- **Captured Scope:** Active foreground root view & initial UI state of `{bundle_id}` after launch.",
        f"- **Screenshots Taken:** {len(screenshot_paths)} screenshot(s) (with {wait_seconds:.1f}s post-launch settle delay).",
        f"- **Appearance:** `{appearance_choice}`",
        f"- **Launch Video:** `{'Recorded (launch.mp4)' if video_recorded else 'Not recorded'}`",
        f"- **Target Simulator:** `{destination}`",
        f"- **Bundle Identifier:** `{bundle_id}`",
        f"- **App Binary:** `{app_path}`",
        f"- **Build Status:** `{'Skipped (reused binary)' if getattr(args, 'no_build', False) else 'Passed'}`",
        f"- **Output Folder:** [{out_dir.name}](file://{out_dir.resolve()})",
        "",
    ]
    if video_recorded:
        report_lines.extend([
            "## Launch Video",
            "",
            f"[▶ Watch Launch & Settle Video]({video_path.name})",
            "",
        ])
    report_lines.append("## Screenshots")
    report_lines.append("")
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
    print("   \033[90mBoots the app on the simulator to check launch layout, element alignment, and safe-area insets. Click any screen to zoom and inspect.\033[0m\n")
    print(f"   \033[1;36m• Target App:\033[0m        \033[1;97m{bundle_id}\033[0m \033[90m({app_path.name})\033[0m")
    print(f"   \033[1;36m• Scope Captured:\033[0m    \033[97mForeground root view & initial UI state after launch\033[0m")
    if video_recorded:
        print(f"   \033[1;36m• Launch Video:\033[0m      \033[1;92m{video_path.name}\033[0m ({markdown_link(video_path.name, video_path)})")
    print(f"   \033[1;36m• Screenshots:\033[0m       \033[1;93m{len(screenshot_paths)} screenshot(s)\033[0m \033[90m({wait_seconds:.1f}s post-launch settle delay)\033[0m")
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
