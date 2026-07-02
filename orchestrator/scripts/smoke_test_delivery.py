#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import subprocess
import json
from pathlib import Path
from datetime import datetime

# Add scripts dir to path
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT, JOBS_DIR, write_json, timestamp, prompt_confirm
from orchestrator.project_config import PROJECT_CONFIG

def run_smoke_delivery():
    global PROJECT_CONFIG
    print("🚀 Starting Smoke Test: Build & Delivery Pipeline...")
    
    # 0. Pre-flight check: Ensure signing configuration is present
    dist_errors = PROJECT_CONFIG.validate_distribution_config()
    if dist_errors:
        print("\n⚠️  Distribution configuration is incomplete. Attempting automatic detection and setup...")
        try:
            from setup_distribution import setup_distribution
            setup_distribution(force=False, root=ROOT)
            
            # Reload project configuration
            from orchestrator.project_config import load_project_config
            PROJECT_CONFIG = load_project_config()
            dist_errors = PROJECT_CONFIG.validate_distribution_config()
        except Exception as e:
            print(f"      - Could not auto-detect configuration: {e}")
            
    if dist_errors:
        config_file = PROJECT_CONFIG.runtime_dir / "project.json"
        print("\n\033[1;91m!!! Error: Distribution configuration is incomplete:\033[0m")
        for err in dist_errors:
            print(f"      - {err}")
        print(f"\n\033[93mYou must configure signing and accounts in the config file before distributing:\033[0m")
        print(f"      \033[1;97m{config_file}\033[0m")
        
        print("\n\033[1;93mManual configuration is required. Please choose one of the options below:\033[0m")
        print("\n\033[1;96mOption A: Headless Auto-Signing (Recommended)\033[0m")
        print("  1. Go to App Store Connect -> Users and Access -> Integrations -> Keys.")
        print("  2. Generate an API Key (Developer or App Manager role) and download the .p8 file.")
        print("  3. Update your .orchestrator/project.json with:")
        print("     - \"asc_key_id\": \"<Key ID>\"")
        print("     - \"asc_issuer_id\": \"<Issuer ID>\"")
        print("     - \"asc_key_path\": \"<Path to your download .p8 file>\"")
        print("\n\033[1;96mOption B: Manual Signing\033[0m")
        print("  1. Create and download an Ad-Hoc/Distribution Provisioning Profile from Apple Developer Portal.")
        print("  2. Install the profile locally on the build machine.")
        print("  3. Set the profile name in your .orchestrator/project.json:")
        print("     - \"provisioning_profile_specifier\": \"<Profile Name>\"")
        print("")
        
        if prompt_confirm("Would you like to run the Distribution Setup now?", default=True):
            print("\n\033[1;96mStarting Interactive Distribution Setup...\033[0m")
            dist_script_path = SCRIPTS_DIR / "setup_distribution.py"
            # Call setup_distribution.py directly in interactive mode
            subprocess.run([sys.executable, str(dist_script_path), "--interactive"], cwd=str(ROOT), stdin=sys.stdin)
            print("\n✅ Setup complete. Please re-run the smoke test to verify.")
            sys.exit(0)
        
        sys.exit(1)
    else:
        print("✅ Distribution configuration is valid.")

    # 1. Setup metadata
    test_id = f"smoke-delivery-{timestamp()}"
    # Just use current branch, don't change anything
    branch_output = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(ROOT)).decode("utf-8").strip()
    branch = branch_output
    job_file = JOBS_DIR / f"{test_id}.json"
    
    print(f"      - Test ID: {test_id}")
    print(f"      - Current Branch: {branch}")

    try:
        # 2. Create Mock Job JSON
        job_data = {
            "job_id": test_id,
            "title": "Smoke Test Delivery",
            "status": "review-needed",
            "branch": branch,
            "issue_number": 9999,
            "builder": "gemini",
            "reviewer": "gemini",
            "planner": "gemini",
            "testers": "enmeskin@gmail.com",
            "groups": "internal-testers"
        }
        write_json(job_file, job_data)
        print(f"      - Created mock job: {job_file.name}")

        # 3. Trigger Actual Delivery
        print("\n🔥 TRIGGERING ACTUAL BUILD AND DELIVERY...")
        print("      - This will use your real Firebase and Xcode configuration.")
        print("      - Expect this to take 5-10 minutes.")
        
        deliver_script = SCRIPTS_DIR / "deliver_build.py"
        # We pass the job file to deliver_build.py and stream output directly
        # We add --testers override to ensure it goes directly to the user
        res = subprocess.call([sys.executable, str(deliver_script), str(job_file)], cwd=str(ROOT), stdout=sys.stdout, stderr=sys.stderr)

        if res == 0:
            print("\n✨ SMOKE TEST SUCCESSFUL!")
            print("      - Build should be appearing on your device soon.")
            print("\n\033[1;96mCheck the logs above for Firebase distribution URLs.\033[0m")
        else:
            print("\n❌ Smoke delivery failed during build/distribution.")
            print("   See the diagnostic above for the fix.")
            # We don't exit immediately because we want to cleanup the mock job
            
        print("\n\033[90mCleaning up the temporary smoke-test job.\033[0m")
        
        # We use a forced final wait here only if we aren't in CI mode
        # But actually, the dev_console run_script handles this better.
        # We'll just do the cleanup and exit.
        
        if prompt_confirm("Cleanup: Delete temporary mock job?", default=True):
            if job_file.exists():
                os.remove(job_file)
            print("      - Temporary job deleted.")
        else:
            print("      - Temporary job file preserved in ai/jobs/.")
            
        if res != 0:
            sys.exit(res)

    except Exception as e:
        print(f"\n!!! Error during smoke test: {e}")
        if job_file.exists():
            if prompt_confirm("Emergency Cleanup: Delete temporary mock job?", default=True):
                os.remove(job_file)
                print("      - Deleted temporary job file.")

if __name__ == "__main__":
    try:
        run_smoke_delivery()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
