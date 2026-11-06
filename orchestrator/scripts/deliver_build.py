#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import sys
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

# Add scripts dir to path for internal imports
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT, read_json, format_markdown_for_terminal, ProgressIndicator, print_header, ensure_keychain_unlocked
from orchestrator.project_config import PROJECT_CONFIG

def send_final_notification(job: dict[str, Any], title: str, branch: str, success: bool):
    job_id = job.get("job_id", "unknown")
    status_str = "SUCCESSFUL" if success else "FAILED"
    color = "\033[1;92m" if success else "\033[1;91m"
    print(f"\n   {color}🔔 Triggering final {status_str} notification for job {job_id}...\033[0m", flush=True)
    try:
        subject = f"Build Delivery {status_str}"
        msg = f"Build delivery for Issue #{job.get('issue_number', 'N/A')} has {status_str.lower()}.\nTitle: {title}\nBranch: {branch}"
        if not success:
            msg += "\n\nPlease check the orchestrator console logs for details."
            
        subprocess.run([sys.executable, str(SCRIPTS_DIR / "notify.py"), subject, msg, job_id], cwd=str(ROOT), check=False, timeout=30)
    except Exception as e:
        print(f"   \033[91m⚠️ Notification system failed: {e}\033[0m", flush=True)

def deliver_build(job_path: Path):
    job = read_json(job_path)
    
    branch = job.get("branch")
    title = job.get("title")
    job_id = job.get("job_id")
    
    if not branch:
        print(f"\033[1;91m!!! Error: No active branch found for job {job_id}. Cannot create a build.\033[0m", flush=True)
        return

    print_header(f"DELIVERING BUILD TO DEVICE: {job_id}")
    print(f"   \033[1;36m• Target Branch:\033[0m \033[1;97m{branch}\033[0m", flush=True)
    print(f"   \033[1;36m• Feature/Bug:\033[0m   \033[97m{title}\033[0m", flush=True)
    
    # Pre-flight check: Auto unlock keychain upfront
    unlocked, keychain_msg = ensure_keychain_unlocked(prompt_if_missing=True)
    if unlocked:
        print(f"   \033[1;92m✓ Keychain status: {keychain_msg}\033[0m", flush=True)
    else:
        print(f"   \033[1;93m⚠️ Keychain status: {keychain_msg}\033[0m", flush=True)
    
    # 0. Pre-flight check: Ensure signing configuration is present
    dist_errors = PROJECT_CONFIG.validate_distribution_config()
    if dist_errors:
        config_file = PROJECT_CONFIG.runtime_dir / "project.json"
        print("\n\033[1;91m!!! Error: Distribution configuration is incomplete:\033[0m", flush=True)
        for err in dist_errors:
            print(f"      - {err}", flush=True)
        print(f"\n\033[93mPlease run 'orchestrator wizard' or configure the following config file:\033[0m", flush=True)
        print(f"      \033[1;97m{config_file}\033[0m", flush=True)
        sys.exit(1)
    
    # 1. Ensure we are on the correct branch
    current_branch = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
    if current_branch != branch:
        print(f"\n   \033[93m🔄 Switching to branch:\033[0m \033[1;97m{branch}\033[0m...", flush=True)
        subprocess.run(["git", "checkout", "-f", branch], cwd=str(ROOT), check=True)
    else:
        print(f"\n   \033[1;92m✓ Branch Status:\033[0m \033[90mAlready on correct branch '{branch}'\033[0m", flush=True)
    
    # 2. Build Release Notes
    build_number = job.get("build_number") or subprocess.check_output(['date', '+%Y%m%d%H%M%S']).decode('utf-8').strip()
    built_at = subprocess.check_output(['date', '+%Y-%m-%d %H:%M:%S']).decode('utf-8').strip()
    release_notes = f"""AI Job: {job_id}
Title: {title}
Branch: {branch}
Build: {build_number}
Built: {built_at}
"""
    
    # 3. Call the existing distribution script
    # We use subprocess.call to allow real-time streaming of Xcode and Firebase logs
    dist_script = ROOT / (PROJECT_CONFIG.distribution_script_path or "scripts/distribute_ios.sh")
    
    if not dist_script.exists():
        print(f"\033[1;91m!!! Error: Distribution script not found at {dist_script}\033[0m", flush=True)
        return

    print(f"\n\033[1;94m🚀 Triggering Distribution Pipeline (Archiving, Exporting, Uploading)...\033[0m", flush=True)
    print("   \033[90mℹ️ Archiving & uploading may take 5-10 minutes. Please wait...\033[0m", flush=True)
    
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    dist_log = log_dir / f"distribution_{job_id}.log"
    print(f"   \033[1;36m• Log File:\033[0m      \033[1;97m{dist_log.relative_to(ROOT)}\033[0m\n", flush=True)
    
    try:
        # Run the bash script and capture output to both console and file
        project_path = None
        if PROJECT_CONFIG.xcode_project:
            project_path = ROOT / PROJECT_CONFIG.xcode_project
        else:
            project_path = next(iter(sorted(ROOT.glob("*.xcodeproj"))), None)
            
        workspace_path = getattr(PROJECT_CONFIG, "xcode_workspace", None)
        if workspace_path:
            workspace_path = ROOT / workspace_path
            
        if not project_path and not workspace_path:
            print("!!! Error: No Xcode project or workspace configured for distribution.")
            return
            
        cmd = ["/bin/bash", str(dist_script)]
        if project_path:
            cmd.extend(["--project", str(project_path)])
        if workspace_path:
            cmd.extend(["--workspace", str(workspace_path)])
            
        if PROJECT_CONFIG.scheme:
            cmd.extend(["--scheme", PROJECT_CONFIG.scheme])
            
        cmd.extend(["--release-notes", release_notes])
        cmd.extend(["--build-number", str(build_number)])
        
        provisioning_profile = getattr(PROJECT_CONFIG, "provisioning_profile_specifier", None)
        if provisioning_profile:
            cmd.extend(["--provisioning-profile", provisioning_profile])
        
        team_id = getattr(PROJECT_CONFIG, "development_team", None)
        if team_id:
            cmd.extend(["--team-id", team_id])
            
        method = getattr(PROJECT_CONFIG, "delivery_method", None)
        if method:
            cmd.extend(["--method", method])
            
        asc_key_id = getattr(PROJECT_CONFIG, "asc_key_id", None)
        if asc_key_id:
            cmd.extend(["--asc-key-id", asc_key_id])
            
        asc_issuer_id = getattr(PROJECT_CONFIG, "asc_issuer_id", None)
        if asc_issuer_id:
            cmd.extend(["--asc-issuer-id", asc_issuer_id])
            
        asc_key_path = getattr(PROJECT_CONFIG, "asc_key_path", None)
        if PROJECT_CONFIG.asc_key_path:
            key_path = ROOT / asc_key_path if not Path(asc_key_path).is_absolute() else Path(asc_key_path)
            cmd.extend(["--asc-key-path", str(key_path)])
        if PROJECT_CONFIG.firebase_plist_path:
            cmd.extend(["--firebase-plist", PROJECT_CONFIG.firebase_plist_path])
        testers = job.get("testers") or getattr(PROJECT_CONFIG, "firebase_testers", None)
        if testers:
            cmd.extend(["--testers", testers])
        groups = job.get("groups") or getattr(PROJECT_CONFIG, "firebase_groups", None)
        if groups:
            cmd.extend(["--groups", groups])
        
        with open(dist_log, "w") as f:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(ROOT),
                text=True,
                bufsize=1
            )
            
            # Setup ProgressIndicator with the initial log message
            indicator = ProgressIndicator(
                label="Distributing build",
                hint=f"Log: {dist_log.relative_to(ROOT)}"
            )
            
            shared = {"line": "Starting build..."}
            
            def read_output():
                for line in process.stdout:
                    f.write(line)
                    f.flush()
                    stripped = line.strip()
                    if stripped:
                        shared["line"] = stripped
                        
            output_thread = threading.Thread(target=read_output, daemon=True)
            output_thread.start()
            
            while process.poll() is None:
                indicator.label = shared["line"]
                indicator.render(force=True)
                time.sleep(0.1)
                
            # Wait for output thread to finish writing any remaining lines
            output_thread.join(timeout=2.0)
            
            # Show final line before clearing
            if shared["line"]:
                indicator.label = shared["line"]
                indicator.render(force=True)
                
            res = process.wait()
            indicator.clear()
        
        if res == 0:
            print("\n✅ Build delivered to Firebase successfully!", flush=True)
            print("      - Finalizing and sending notifications...", flush=True)
            send_final_notification(job, title, branch, True)
            print("\n✨ ALL DONE.", flush=True)
            sys.exit(0)
        else:
            print(f"\n❌ Distribution failed (exit {res}).", flush=True)

            # Diagnose common Xcode / Apple Developer login issues
            if dist_log.exists():
                log_content = dist_log.read_text()
                if "Unable to log in with account" in log_content or "rejected" in log_content:
                    print("\n\033[1;91mXcode Apple ID session needs attention\033[0m")
                    print("Xcode could not authenticate with the Apple Developer Portal.")
                    print("\nNext steps:")
                    print("  1. Open Xcode > Settings > Accounts.")
                    print("  2. Re-authenticate your Apple ID, including 2FA if prompted.")
                    print("  3. Re-run the delivery smoke test.")
                elif "User interaction is not allowed" in log_content or "errSecInternalComponent" in log_content:
                    print("\n\033[1;91mKeychain is locked / User interaction not allowed\033[0m")
                    print("Xcode could not access your signing certificate because the login keychain is locked.")
                    print("\nNext steps:")
                    print("  1. Run 'orchestrator console'.")
                    print("  2. Go to Configuration & Tools -> [D] Firebase App Distro -> [K] Configure Headless Signing.")
                    print("  3. Provide your keychain password to authorize automatic unlocking during builds.")
                elif "failed to distribute to testers" in log_content or "HTTP Error: 404" in log_content:
                    msg = f"""# Firebase distribution failed (HTTP 404)

The Firebase App Distribution release uploaded successfully, but the distribution to testers or groups failed. 
This usually means a specified tester email or group does not exist in your Firebase project console.

**Configured Recipients:**
* **Testers:** `{testers or '<none>'}`
* **Groups:** `{groups or '<none>'}`

**Next steps:**
1. Open your **Firebase Console** -> **App Distribution**.
2. Verify that the testers are added or that the group exists (e.g. create the group or invite the testers).
3. If you want to configure different default testers or groups for this project, you can add them to your `.orchestrator/project.json`:
   ```json
   {{
     "firebase_testers": "tester1@example.com,tester2@example.com",
     "firebase_groups": "internal-testers"
   }}
   ```
"""
                    print(format_markdown_for_terminal(msg))
                elif "No Account for Team" in log_content or "No profiles for" in log_content or "No signing certificate" in log_content:
                    failed_team = None
                    team_match = re.search(r"No Account for Team \"([^\"]+)\"", log_content)
                    if team_match:
                        failed_team = team_match.group(1)
                    
                    missing_bundle_id = None
                    bundle_match = re.search(r"No profiles for '([^']+)' were found", log_content)
                    if not bundle_match:
                        bundle_match = re.search(r"\"([^\"\s]+)\" requires a provisioning profile", log_content)
                    if bundle_match:
                        missing_bundle_id = bundle_match.group(1)
                    
                    # Gather details about current configuration
                    has_asc = all([PROJECT_CONFIG.asc_key_id, PROJECT_CONFIG.asc_issuer_id, PROJECT_CONFIG.asc_key_path])
                    
                    bundle_info = f"\nXcode was looking for a profile matching Bundle ID: **{missing_bundle_id}**\n" if missing_bundle_id else ""
                    team_info = f"\nXcode was looking for Team ID: **{failed_team}**\n" if failed_team else ""

                    if has_asc:
                        # Mismatch warning
                        mismatch_warning = ""
                        if failed_team and PROJECT_CONFIG.development_team and failed_team != PROJECT_CONFIG.development_team:
                            mismatch_warning = f"""
> [!WARNING]
> **Team ID Mismatch Detected!**
> Your `.orchestrator/project.json` is configured with `development_team`: `{PROJECT_CONFIG.development_team}`, 
> but Xcode is trying to build for Team: `{failed_team}`. 
> Please update the `development_team` in `.orchestrator/project.json` to `{failed_team}` (and verify your Xcode project target build settings).
"""
                        
                        msg = f"""# Signing setup needs attention

Xcode was unable to sign your app using the configured App Store Connect API Key.
{team_info}{bundle_info}{mismatch_warning}
Please check the following common issues:

1. **Incorrect Team ID**: Ensure the Team ID in `.orchestrator/project.json` (`{PROJECT_CONFIG.development_team or '<missing>'}`) matches your App Store Connect team and your Xcode project settings.
2. **Insecure Key Location**: xcodebuild ignores/rejects `.p8` files unless they are stored in `~/.private_keys/` or `~/.appstoreconnect/private_keys/`. Currently configured to: `{PROJECT_CONFIG.asc_key_path}`.
3. **Invalid API Key**: Double check that the `asc_key_id` (`{PROJECT_CONFIG.asc_key_id}`) and `asc_issuer_id` (`{PROJECT_CONFIG.asc_issuer_id}`) are correct, and that the API key has the **Developer** or **App Manager** role on App Store Connect.
"""
                    else:
                        msg = f"""# Signing setup needs attention

Xcode could not find a certificate or provisioning profile for this app.
{team_info}{bundle_info}
Please configure code signing using one of the two options below:

## Option A: Headless Auto-Signing (Recommended)

This is recommended for remote workers, CI, or automated builds to prevent Xcode login prompts:

1. Go to **App Store Connect** -> **Users and Access** -> **Integrations** -> **Keys**.
2. Generate an API Key (with the **Developer** or **App Manager** role) and download the `.p8` key file.
3. Update your `.orchestrator/project.json` with the key details:

```json
{{
  "asc_key_id": "{PROJECT_CONFIG.asc_key_id or '<Key ID>'}",
  "asc_issuer_id": "{PROJECT_CONFIG.asc_issuer_id or '<Issuer ID>'}",
  "asc_key_path": "{PROJECT_CONFIG.asc_key_path or '<Path to your .p8 file>'}"
}}
```

## Option B: Manual Signing

If you prefer to sign builds manually:

1. Create and download an **Ad-Hoc / Distribution Provisioning Profile** from the Apple Developer Portal.
2. Install the profile locally on the build machine by double-clicking it.
3. Add the profile name to `.orchestrator/project.json`:

```json
{{
  "provisioning_profile_specifier": "{PROJECT_CONFIG.provisioning_profile_specifier or '<Profile Name>'}"
}}
```
"""
                    print(format_markdown_for_terminal(msg))

            send_final_notification(job, title, branch, False)
            sys.exit(res)
            
    except KeyboardInterrupt:
        print("\n!!! Distribution interrupted by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n!!! Error during distribution: {e}")
        sys.exit(1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("job_file", help="Path to the job JSON file")
    args = parser.parse_args()

    deliver_build(Path(args.job_file))

if __name__ == "__main__":
    main()
