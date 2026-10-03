#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT, JOBS_DIR, write_json, timestamp, prompt_confirm, print_header, ensure_keychain_unlocked
from orchestrator.project_config import PROJECT_CONFIG

def run_smoke_delivery():
    print_header("LIVE BUILD & FIREBASE DELIVERY")
    
    # 0. Pre-flight check: Ensure signing configuration is present
    print("\033[1;36m[STEP 1/3]\033[0m \033[1;97mPre-flight Configuration Check\033[0m", flush=True)
    dist_errors = PROJECT_CONFIG.validate_distribution_config()
    if dist_errors:
        config_file = PROJECT_CONFIG.runtime_dir / "project.json"
        print("\n\033[1;91m!!! Error: Distribution configuration is incomplete:\033[0m", flush=True)
        for err in dist_errors:
            print(f"      - {err}", flush=True)
        print(f"\n\033[93mYou must configure signing and accounts in the config file before distributing:\033[0m", flush=True)
        print(f"      \033[1;97m{config_file}\033[0m", flush=True)
        
        print("\n\033[1;93mManual configuration is required. Please choose one of the options below:\033[0m", flush=True)
        print("\n\033[1;96mOption A: Headless Auto-Signing (Recommended)\033[0m", flush=True)
        print("  1. Go to App Store Connect -> Users and Access -> Integrations -> Keys.", flush=True)
        print("  2. Generate an API Key (Developer or App Manager role) and download the .p8 file.", flush=True)
        print("  3. Update your .orchestrator/project.json with:", flush=True)
        print("     - \"asc_key_id\": \"<Key ID>\"", flush=True)
        print("     - \"asc_issuer_id\": \"<Issuer ID>\"", flush=True)
        print("     - \"asc_key_path\": \"<Path to your download .p8 file>\"", flush=True)
        print("\n\033[1;96mOption B: Manual Signing\033[0m", flush=True)
        print("  1. Create and download an Ad-Hoc/Distribution Provisioning Profile from Apple Developer Portal.", flush=True)
        print("  2. Install the profile locally on the build machine.", flush=True)
        print("  3. Set the profile name in your .orchestrator/project.json:", flush=True)
        print("     - \"provisioning_profile_specifier\": \"<Profile Name>\"", flush=True)
        print("", flush=True)
        
        wizard_desc = "Runs the interactive project configuration wizard to set up Apple Team ID, Firebase credentials, and provisioning profiles."
        if prompt_confirm("Would you like to run the Setup Wizard now?", default=True, description=wizard_desc, clear_screen=False):
            print("\n\033[1;96mStarting Orchestrator Wizard...\033[0m", flush=True)
            cli_path = SCRIPTS_DIR.parent / "cli.py"
            res = subprocess.run([sys.executable, str(cli_path), "wizard"], cwd=str(ROOT))
            if res.returncode == 0:
                print("\n\033[1;92m✅ Wizard complete. Please re-run the smoke test to verify.\033[0m", flush=True)
            else:
                print(f"\n\033[1;91m❌ Setup Wizard failed/exited with code {res.returncode}. Please check the error above.\033[0m", flush=True)
        
        sys.exit(1)
    else:
        print("   \033[1;92m✅ Distribution configuration is valid.\033[0m\n", flush=True)

    unlocked, keychain_msg = ensure_keychain_unlocked(prompt_if_missing=True)
    if unlocked:
        print(f"   \033[1;92m✓ Keychain status: {keychain_msg}\033[0m", flush=True)
    else:
        print(f"   \033[1;93m⚠️ Keychain status: {keychain_msg}\033[0m", flush=True)

    # 1. Setup metadata
    test_id = f"quick-delivery-{timestamp()}"
    # Just use current branch, don't change anything
    branch_output = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
    branch = branch_output
    job_file = JOBS_DIR / f"{test_id}.json"
    
    print("\033[1;36m[STEP 2/3]\033[0m \033[1;97mPreparing Delivery Metadata\033[0m", flush=True)
    print(f"   \033[1;36m• Delivery ID:\033[0m    \033[1;97m{test_id}\033[0m", flush=True)
    print(f"   \033[1;36m• Current Branch:\033[0m \033[97m{branch}\033[0m", flush=True)

    try:
        # 2. Create Mock Job JSON
        title = os.environ.get("DISTRIBUTION_RELEASE_NOTES") or "Quick Console Delivery"
        job_data = {
            "job_id": test_id,
            "title": title,
            "delivery_kind": "quick",
            "status": "review-needed",
            "branch": branch,
            "builder": "console",
            "reviewer": "console",
            "testers": os.environ.get("FIREBASE_TESTERS", ""),
            "groups": os.environ.get("FIREBASE_GROUPS", "")
        }
        write_json(job_file, job_data)
        print(f"   \033[1;36m• Runtime Record:\033[0m \033[90m{job_file.name}\033[0m\n", flush=True)

        # 3. Trigger Actual Delivery
        print("\033[1;36m[STEP 3/3]\033[0m \033[1;93m🔥 Triggering Build & Delivery Pipeline\033[0m", flush=True)
        print("   \033[90mℹ️ Using live Firebase and Xcode configuration (estimated time: 5-10 minutes)\033[0m\n", flush=True)
        
        deliver_script = SCRIPTS_DIR / "deliver_build.py"
        # We pass the job file to deliver_build.py and stream output directly
        res = subprocess.call([sys.executable, str(deliver_script), str(job_file)], cwd=str(ROOT), stdout=sys.stdout, stderr=sys.stderr)

        if res == 0:
            print_header("✨ Delivery Successful!")
            print("   \033[97mBuild should be appearing on your registered device soon.\033[0m\n", flush=True)
        else:
            print_header("❌ Delivery Failed")
            print("   \033[97mSee the diagnostic messages above for details and fix instructions.\033[0m\n", flush=True)
            
        # Auto-cleanup temporary mock job
        if job_file.exists():
            os.remove(job_file)
            print(f"   \033[92m✓ Cleaned up temporary delivery record ({job_file.name}).\033[0m\n", flush=True)
            
        if res != 0:
            sys.exit(res)

    except Exception as e:
        print(f"\n\033[1;91m!!! Error during delivery: {e}\033[0m", flush=True)
        if job_file.exists():
            os.remove(job_file)
            print(f"   \033[92m✓ Cleaned up temporary delivery record ({job_file.name}).\033[0m\n", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    try:
        run_smoke_delivery()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
