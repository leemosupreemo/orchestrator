#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from common import (
    JOBS_DIR,
    OUTPUT_DIR,
    LOGS_DIR,
    ROOT,
    CONFIG_DIR,
    read_json,
    print_phase,
    format_file_link,
    prompt_radio,
    prompt_input,
    BackException,
)
from reference_artifacts import reference_context


def _format_size(num_bytes: int) -> str:
    """Formats bytes into human-readable size string."""
    size = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024.0 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def detect_export_destinations() -> dict[str, dict[str, Any]]:
    """Discovers available local and cloud storage destinations on macOS / Linux."""
    home = Path.home()
    destinations: dict[str, dict[str, Any]] = {}

    # 1. iCloud Drive (macOS)
    icloud_root = home / "Library/Mobile Documents/com~apple~CloudDocs"
    if icloud_root.exists():
        icloud_dir = icloud_root / "Orchestrator"
        destinations["icloud"] = {
            "name": "iCloud Drive (Orchestrator/)",
            "path": icloud_dir,
            "icon": "☁️",
            "available": True,
            "desc": "Syncs automatically across Apple devices (Mac, iPhone, iPad)",
        }
    else:
        destinations["icloud"] = {
            "name": "iCloud Drive [Not Enabled]",
            "path": None,
            "icon": "☁️",
            "available": False,
            "desc": "iCloud Drive is not enabled on this system",
        }

    # 2. Google Drive
    gdrive_dir = None
    settings_path = CONFIG_DIR / "settings.json"
    settings = read_json(settings_path) if settings_path.exists() else {}
    if settings.get("google_drive_path"):
        custom_gdrive = Path(settings["google_drive_path"]).expanduser().resolve()
        if custom_gdrive.exists():
            gdrive_dir = custom_gdrive / "Orchestrator" if not custom_gdrive.name.lower() == "orchestrator" else custom_gdrive

    if not gdrive_dir:
        cloud_storage = home / "Library/CloudStorage"
        if cloud_storage.exists():
            for item in sorted(cloud_storage.glob("GoogleDrive-*")):
                my_drive = item / "My Drive"
                if my_drive.exists():
                    gdrive_dir = my_drive / "Orchestrator"
                    break
                elif item.exists():
                    gdrive_dir = item / "Orchestrator"
                    break

    if not gdrive_dir:
        for candidate in [home / "Google Drive/My Drive", home / "Google Drive"]:
            if candidate.exists():
                gdrive_dir = candidate / "Orchestrator"
                break

    if gdrive_dir:
        destinations["gdrive"] = {
            "name": "Google Drive (My Drive/Orchestrator/)",
            "path": gdrive_dir,
            "icon": "☁️",
            "available": True,
            "desc": "Syncs automatically to your Google Drive account",
        }
    else:
        destinations["gdrive"] = {
            "name": "Google Drive [Not Detected]",
            "path": None,
            "icon": "☁️",
            "available": False,
            "desc": "Google Drive desktop app folder was not detected",
        }

    # 3. macOS Downloads folder
    downloads_dir = home / "Downloads"
    destinations["downloads"] = {
        "name": "Downloads Folder (~/Downloads/)",
        "path": downloads_dir if downloads_dir.exists() else home,
        "icon": "📥",
        "available": downloads_dir.exists(),
        "desc": "Standard user Downloads folder",
    }

    # 4. Local project exports
    local_dir = Path.cwd() / "exports"
    destinations["local"] = {
        "name": "Local Project Workspace (./exports/)",
        "path": local_dir,
        "icon": "📁",
        "available": True,
        "desc": "Saves directly inside current project directory",
    }

    return destinations


def export_job(job_path_str: str, output_zip: str | None = None, destination: str | None = None) -> Path | None:
    job_path = Path(job_path_str).resolve()
    if not job_path.exists():
        print(f"\033[1;91mError: Job file {job_path} not found.\033[0m")
        return None

    with open(job_path, "r") as f:
        job = json.load(f)

    job_id = job.get("job_id", job_path.stem)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    default_zip_name = f"export-{job_id}-{timestamp}.zip"
    
    discovered = detect_export_destinations()
    dest_id = "local"
    dest_label = "Local Project Workspace"
    target_dir = Path.cwd() / "exports"
    
    # 1. Resolve explicit output_zip argument if given
    if output_zip:
        out_candidate = Path(output_zip).expanduser()
        if out_candidate.is_dir() or str(output_zip).lower() in {"icloud", "gdrive", "googledrive", "google-drive", "downloads", "local"}:
            destination = str(output_zip)
            output_zip = None
        else:
            output_path = out_candidate.resolve()
            dest_id = "custom"
            dest_label = str(output_path.parent)
            target_dir = output_path.parent
    
    # 2. Resolve destination parameter if provided
    if not output_zip and destination:
        dest_norm = destination.strip().lower()
        if dest_norm in {"icloud", "cloud"}:
            dest_info = discovered["icloud"]
            if dest_info["available"] and dest_info["path"]:
                dest_id = "icloud"
                dest_label = dest_info["name"]
                target_dir = dest_info["path"]
            else:
                print("\033[1;93m⚠️  iCloud Drive not active on this system. Falling back to local project.\033[0m")
                target_dir = Path.cwd() / "exports"
        elif dest_norm in {"gdrive", "googledrive", "google-drive", "drive"}:
            dest_info = discovered["gdrive"]
            if dest_info["available"] and dest_info["path"]:
                dest_id = "gdrive"
                dest_label = dest_info["name"]
                target_dir = dest_info["path"]
            else:
                print("\033[1;93m⚠️  Google Drive not detected on this system. Falling back to local project.\033[0m")
                target_dir = Path.cwd() / "exports"
        elif dest_norm in {"downloads", "download"}:
            dest_info = discovered["downloads"]
            dest_id = "downloads"
            dest_label = dest_info["name"]
            target_dir = dest_info["path"]
        elif dest_norm in {"local", "project"}:
            dest_id = "local"
            dest_label = discovered["local"]["name"]
            target_dir = discovered["local"]["path"]
        else:
            custom_dir = Path(destination).expanduser().resolve()
            dest_id = "custom"
            dest_label = str(custom_dir)
            target_dir = custom_dir

    # 3. Interactive prompt if no explicit path/destination was supplied
    if not output_zip and not destination and sys.stdin.isatty():
        options: list[str] = []
        option_map: dict[str, tuple[str, str, Path]] = {}

        if discovered["icloud"]["available"] and discovered["icloud"]["path"]:
            opt = f"☁️  iCloud Drive (Orchestrator/) \033[90m(Syncs across Mac, iPhone, iPad)\033[0m"
            options.append(opt)
            option_map[opt] = ("icloud", "iCloud Drive (Orchestrator/)", discovered["icloud"]["path"])

        if discovered["gdrive"]["available"] and discovered["gdrive"]["path"]:
            opt = f"☁️  Google Drive (My Drive/Orchestrator/) \033[90m(Syncs to Google Drive)\033[0m"
            options.append(opt)
            option_map[opt] = ("gdrive", "Google Drive (My Drive/Orchestrator/)", discovered["gdrive"]["path"])

        opt_local = f"📁 Local Project Workspace \033[90m(./exports/)\033[0m"
        options.append(opt_local)
        option_map[opt_local] = ("local", "Local Project (./exports/)", discovered["local"]["path"])

        if discovered["downloads"]["available"]:
            opt_dl = f"📥 macOS Downloads \033[90m(~/Downloads/)\033[0m"
            options.append(opt_dl)
            option_map[opt_dl] = ("downloads", "Downloads Folder (~/Downloads/)", discovered["downloads"]["path"])

        opt_custom = f"✏️  Custom Directory Path \033[90m(Specify custom folder)\033[0m"
        options.append(opt_custom)
        option_map[opt_custom] = ("custom", "Custom Directory", Path.home())

        default_opt = options[0]
        try:
            import re
            choice = prompt_radio("Select Export Destination:", options, default=default_opt, clear_screen=False)
            choice_clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", choice).strip().lower()
            dest_info = option_map.get(choice)
            if not dest_info:
                for opt_key, info in option_map.items():
                    key_clean = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", opt_key).strip().lower()
                    if choice_clean == key_clean or choice_clean in key_clean or key_clean in choice_clean or info[0] in choice_clean:
                        dest_info = info
                        break
            if dest_info:
                dest_id, dest_label, target_dir = dest_info
                if dest_id == "custom":
                    entered = prompt_input("Enter destination directory path:", default=str(Path.home()))
                    if entered:
                        target_dir = Path(entered).expanduser().resolve()
                        dest_label = str(target_dir)
        except BackException:
            print("\nExport cancelled.")
            return None

    if not output_zip:
        target_dir.mkdir(parents=True, exist_ok=True)
        output_path = target_dir / default_zip_name
    else:
        output_path = Path(output_zip).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

    print_phase("exporting", subtext=job_id)
    print(f"\n📦 \033[1;97mCreating export archive:\033[0m \033[96m{output_path.name}\033[0m")

    from common import format_investigation_history
    inv_str = format_investigation_history(job)

    file_count = 0
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        # 1. Add Job JSON
        zipf.write(job_path, arcname=f"{job_id}/job.json")
        file_count += 1
        print(f"  • 📄 \033[1;97mJob Definition\033[0m     \033[90m(job.json)\033[0m")

        # 2. Add Output Directory (Brief, Plan, Builder Summary, Logs)
        job_output_dir = OUTPUT_DIR / job_id
        output_files_count = 0
        if job_output_dir.exists():
            for file in job_output_dir.rglob("*"):
                if file.is_file():
                    arcname = f"{job_id}/output/{file.relative_to(job_output_dir)}"
                    zipf.write(file, arcname=arcname)
                    output_files_count += 1
                    file_count += 1
            print(f"  • 📁 \033[1;97mOutput Artifacts\033[0m   \033[90m({output_files_count} files: brief, plan, logs)\033[0m")
        else:
            print(f"  • 📁 \033[1;90mOutput Artifacts   (None recorded)\033[0m")

        # 3. Create a manifest or summary for easy reading
        manifest = [
            f"Job ID: {job_id}",
            f"Title: {job.get('title')}",
            f"Status: {job.get('status')}",
            f"Export Date: {datetime.now().isoformat()}",
            f"\nPlan Summary:\n{json.dumps(job.get('plan', {}).get('summary', 'No summary'), indent=2)}",
            f"\nReference Artifacts:\n{reference_context(job) or 'None'}",
        ]
        if inv_str:
            manifest.append(f"\nInvestigations & CLI Notes:\n{inv_str}")
        zipf.writestr(f"{job_id}/manifest.txt", "\n".join(manifest))
        file_count += 1
        print(f"  • 📝 \033[1;97mContext Manifest\033[0m   \033[90m(manifest.txt)\033[0m")

    size_bytes = output_path.stat().st_size if output_path.exists() else 0
    size_str = _format_size(size_bytes)
    clickable_path = format_file_link(output_path, label=str(output_path))
    
    print("\n\033[1;92m╭────────────────────────────────────────────────────────────────────────╮\033[0m")
    print("\033[1;92m│ ✅ Export Package Ready                                                │\033[0m")
    print("\033[1;92m╰────────────────────────────────────────────────────────────────────────╯\033[0m")
    print(f"  • 📍 \033[1;97mDestination:\033[0m  \033[96m{dest_label}\033[0m")
    print(f"  • 📦 \033[1;97mArchive:\033[0m      \033[1;97m{output_path.name}\033[0m \033[92m({size_str}, {file_count} files)\033[0m")
    print(f"  • 🔗 \033[1;97mFile Link:\033[0m    \033[4;96m{clickable_path}\033[0m")
    if dest_id == "icloud":
        print(f"  • ☁️  \033[1;97mCloud Sync:\033[0m   \033[90mSyncing automatically across your Apple devices (iCloud Drive)\033[0m")
    elif dest_id == "gdrive":
        print(f"  • ☁️  \033[1;97mCloud Sync:\033[0m   \033[90mSyncing automatically to your Google Drive\033[0m")
    print()

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Export all context for a job into a ZIP archive with iCloud / Google Drive support.")
    parser.add_argument("job_path", help="Path to the job JSON file")
    parser.add_argument("--output", "-o", help="Output ZIP file path")
    parser.add_argument("--destination", "-d", help="Target destination: icloud, gdrive, downloads, local, or directory path")
    
    args = parser.parse_args()
    export_job(args.job_path, output_zip=args.output, destination=args.destination)


if __name__ == "__main__":
    main()
