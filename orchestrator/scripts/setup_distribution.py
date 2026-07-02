#!/usr/bin/env python3
import os
import argparse
import plistlib
import re
import subprocess
from pathlib import Path
import json

# Add parent dir to path to import from orchestrator
import sys
SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(SCRIPTS_DIR))

from common import ROOT, write_json, read_json, PROJECT_CONFIG

DIST_SCRIPT_TEMPLATE = r"""#!/bin/bash
set -e

# --- Configuration ---
PROJECT_PATH=""
WORKSPACE_PATH=""
SCHEME=""
CONFIGURATION="Release"
PROVISIONING_PROFILE_SPECIFIER=""
DEVELOPMENT_TEAM=""
EXPORT_METHOD="ad-hoc"
ASC_KEY_ID=""
ASC_ISSUER_ID=""
ASC_KEY_PATH=""
FIREBASE_PLIST_PATH=""
RELEASE_NOTES="AI Generated Build"
TESTERS=""
GROUPS=""

while [[ "$#" -gt 0 ]]; do
    case $1 in
        --project) PROJECT_PATH="$2"; shift ;;
        --workspace) WORKSPACE_PATH="$2"; shift ;;
        --scheme) SCHEME="$2"; shift ;;
        --configuration) CONFIGURATION="$2"; shift ;;
        --provisioning-profile) PROVISIONING_PROFILE_SPECIFIER="$2"; shift ;;
        --team-id) DEVELOPMENT_TEAM="$2"; shift ;;
        --method) EXPORT_METHOD="$2"; shift ;;
        --asc-key-id) ASC_KEY_ID="$2"; shift ;;
        --asc-issuer-id) ASC_ISSUER_ID="$2"; shift ;;
        --asc-key-path) ASC_KEY_PATH="$2"; shift ;;
        --firebase-plist) FIREBASE_PLIST_PATH="$2"; shift ;;
        --release-notes) RELEASE_NOTES="$2"; shift ;;
        --testers) TESTERS="$2"; shift ;;
        --groups) GROUPS="$2"; shift ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

if [ -z "$PROJECT_PATH" ] && [ -z "$WORKSPACE_PATH" ]; then
    echo "Error: --project or --workspace is required."
    exit 1
fi

if [ -z "$SCHEME" ]; then
    if [ -n "$PROJECT_PATH" ]; then
        SCHEME=$(basename "$PROJECT_PATH" .xcodeproj)
    elif [ -n "$WORKSPACE_PATH" ]; then
        SCHEME=$(basename "$WORKSPACE_PATH" .xcworkspace)
    fi
fi
ARCHIVE_PATH="/tmp/${SCHEME}.xcarchive"
EXPORT_PATH="/tmp/${SCHEME}_export"
IPA_PATH="${EXPORT_PATH}/${SCHEME}.ipa"

if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" ~/Library/Keychains/login.keychain-db >/dev/null 2>&1 || true
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" ~/Library/Keychains/login.keychain-db >/dev/null 2>&1 || true
fi

echo "🚀 Starting Distribution for $SCHEME..."

# 1. Archive
echo "📦 Archiving project..."

AUTH_FLAGS=""
if [ -n "$ASC_KEY_ID" ] && [ -n "$ASC_ISSUER_ID" ] && [ -n "$ASC_KEY_PATH" ]; then
    AUTH_FLAGS="-authenticationKeyID $ASC_KEY_ID -authenticationKeyIssuerID $ASC_ISSUER_ID -authenticationKeyPath $ASC_KEY_PATH"
fi

if [ -n "$WORKSPACE_PATH" ]; then
    if [ -n "$PROVISIONING_PROFILE_SPECIFIER" ]; then
        xcodebuild archive \
            -workspace "$WORKSPACE_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CODE_SIGN_STYLE=Manual \
            PROVISIONING_PROFILE_SPECIFIER="$PROVISIONING_PROFILE_SPECIFIER" \
            ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -workspace "$WORKSPACE_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    fi
else
    if [ -n "$PROVISIONING_PROFILE_SPECIFIER" ]; then
        xcodebuild archive \
            -project "$PROJECT_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CODE_SIGN_STYLE=Manual \
            PROVISIONING_PROFILE_SPECIFIER="$PROVISIONING_PROFILE_SPECIFIER" \
            ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -project "$PROJECT_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            ${DEVELOPMENT_TEAM:+DEVELOPMENT_TEAM="$DEVELOPMENT_TEAM"} \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    fi
fi

# 2. Export IPA
echo "📤 Exporting IPA..."
xcodebuild -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportOptionsPlist "scripts/ExportOptions.plist" \
    -exportPath "$EXPORT_PATH" \
    $AUTH_FLAGS \
    -allowProvisioningUpdates

# 3. Firebase Upload
echo "🔥 Uploading to Firebase App Distribution..."

GS_INFO_PATH=""
if [ -n "$FIREBASE_PLIST_PATH" ]; then
    GS_INFO_PATH="$FIREBASE_PLIST_PATH"
else
    # Extract App ID from GoogleService-Info.plist via guessing
    GS_INFO_PATH="${PROJECT_PATH%.*}/GoogleService-Info.plist"
    if [ ! -f "$GS_INFO_PATH" ]; then
        # Try one level up if not in project dir
        GS_INFO_PATH="$(dirname "$PROJECT_PATH")/GoogleService-Info.plist"
    fi
fi

if [ -f "$GS_INFO_PATH" ]; then
    APP_ID=$(/usr/libexec/PlistBuddy -c "Print :GOOGLE_APP_ID" "$GS_INFO_PATH")
    echo "      - App ID: $APP_ID"
    firebase appdistribution:distribute "$IPA_PATH" \
        --app "$APP_ID" \
        --release-notes "$RELEASE_NOTES" \
        $( [ ! -z "$TESTERS" ] && echo "--testers $TESTERS" ) \
        $( [ ! -z "$GROUPS" ] && echo "--groups $GROUPS" )
else
    echo "❌ Missing GoogleService-Info.plist: $GS_INFO_PATH"
    echo "      - You can specify the path with --firebase-plist"
    exit 1
fi

echo "✅ Distribution Complete!"
"""

EXPORT_OPTIONS_TEMPLATE = r"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>method</key>
	<string>{method}</string>
	<key>signingStyle</key>
	<string>automatic</string>
	<key>thinning</key>
	<string>&lt;none&gt;</string>
{team_id_entry}
</dict>
</plist>
"""

def setup_distribution(force=False, firebase_plist=None, provisioning_profile=None, team_id=None, method=None, asc_key_id=None, asc_issuer_id=None, asc_key_path=None, root=None, interactive=False):
    if root is None:
        from orchestrator.project_config import find_project_root
        root = find_project_root()

    root = Path(os.fspath(root)).resolve()
    print(f"🛠️  Setting up distribution for {root.name}...")
    
    from common import prompt_input, prompt_confirm, prompt_password, BackException
    
    # We use a helper to get config values safely without relying on a global PROJECT_CONFIG 
    # which might be bound to a different root during tests
    def get_conf_val(key, default=None):
        project_json = root / ".orchestrator" / "project.json"
        if project_json.exists():
            try:
                return json.loads(project_json.read_text(encoding="utf-8")).get(key, default)
            except: pass
        return default

    detected_team_id, detected_method = detect_identity_info()
    
    if interactive:
        print("\n\033[1;96m--- iOS Distribution Setup ---\033[0m")
        print("\033[90mThis will configure your project for automated Firebase builds.\033[0m")
        
        try:
            print("\n\033[1;97m1. Firebase Configuration\033[0m")
            print("   The GoogleService-Info.plist contains your Firebase App ID and project secrets.")
            firebase_plist = firebase_plist or prompt_input("Firebase plist path", default=get_conf_val("firebase_plist_path", f"{root.name}/GoogleService-Info.plist"))
            
            print("\n\033[1;97m2. Apple Development Team ID\033[0m")
            print("   Your 10-character Team ID (e.g., 'ABC123DEFG') links the build to your Apple Developer account.")
            print("   \033[90mFind this in: Xcode -> Target -> Signing & Capabilities -> Team (under the name).\033[0m")
            team_id = team_id or prompt_input("Apple Development Team ID", default=get_conf_val("development_team", detected_team_id))
            
            print("\n\033[1;97m3. Distribution Method\033[0m")
            print("   'ad-hoc' is standard for Firebase. 'development' is for internal testing.")
            method = method or prompt_input("Method (ad-hoc, development)", default=get_conf_val("delivery_method", detected_method or "debugging"))
            
            print("\n\033[1;97m4. Signing Credentials\033[0m")
            print("   Automated builds need a way to sign the app without manual Xcode intervention.")
            if prompt_confirm("Use App Store Connect API keys? (Recommended for automation)", default=False):
                print("\n   \033[1;96mApp Store Connect API Keys:\033[0m")
                print("   Create these at: \033[4;96mhttps://appstoreconnect.apple.com/access/api\033[0m")
                print("   These allow the CLI to download profiles and sign apps securely.")
                asc_key_id = asc_key_id or prompt_password("ASC Key ID", placeholder="e.g. 2X9R4J3A")
                asc_issuer_id = asc_issuer_id or prompt_password("ASC Issuer ID", placeholder="UUID from ASC")
                asc_key_path = asc_key_path or prompt_input("Path to .p8 Key File", default="./AuthKey_ABC.p8")
            else:
                print("\n   \033[1;93mManual Signing Fallback:\033[0m")
                print("   You must provide the exact name of your Provisioning Profile.")
                print("   \033[90mFind this in: Xcode -> Target -> Build Settings -> Search 'Provisioning Profile'.\033[0m")
                detected_profile = detect_provisioning_profile_specifier(root)
                provisioning_profile = provisioning_profile or prompt_input("Provisioning Profile Specifier", default=get_conf_val("provisioning_profile_specifier", detected_profile))
                
                if not provisioning_profile:
                    print("\033[1;91mWarning: Provisioning Profile is required for manual signing. Builds may fail.\033[0m")
            
            print("\n\033[1;97m5. Headless Keychain Unlocking\033[0m")
            print("   To avoid macOS security prompts (UI popups) during builds, the CLI can")
            print("   automatically unlock your keychain using your login password.")
            if prompt_confirm("Enable automated keychain unlocking?", default=True):
                # Trigger dev_console.py keychain-setup
                from common import SCRIPTS_DIR
                subprocess.run([sys.executable, str(SCRIPTS_DIR / "dev_console.py"), "keychain-setup"], cwd=str(root))
        except BackException:
            print("\nSetup cancelled.")
            return

    provisioning_profile_specifier = provisioning_profile or detect_provisioning_profile_specifier(root)
    team_id = team_id or get_conf_val("development_team") or detected_team_id
    method = method or get_conf_val("delivery_method") or detected_method or "ad-hoc"

    if team_id:
        print(f"  ✅ Using Team ID: {team_id}")
    if method:
        print(f"  ✅ Using Method:  {method}")

    asc_key_id = asc_key_id or get_conf_val("asc_key_id")
    asc_issuer_id = asc_issuer_id or get_conf_val("asc_issuer_id")
    asc_key_path = asc_key_path or get_conf_val("asc_key_path")

    # 1. Create scripts directory
    scripts_dir = root / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    # 2. Create distribute_ios.sh
    dist_script = scripts_dir / "distribute_ios.sh"
    if not dist_script.exists() or force:
        dist_script.write_text(DIST_SCRIPT_TEMPLATE)
        dist_script.chmod(0o755)
        print(f"  ✅ Created {dist_script.relative_to(root)}")
    else:
        print(f"  ⏭️  {dist_script.relative_to(root)} already exists. Use --force to overwrite.")
        
    # 3. Create ExportOptions.plist
    export_opts = scripts_dir / "ExportOptions.plist"
    if not export_opts.exists() or force:
        team_id_entry = f"\t<key>teamID</key>\n\t<string>{team_id}</string>" if team_id else ""
        export_opts.write_text(EXPORT_OPTIONS_TEMPLATE.format(method=method, team_id_entry=team_id_entry))
        print(f"  ✅ Created {export_opts.relative_to(root)}")
    else:
        print(f"  ⏭️  {export_opts.relative_to(root)} already exists. Use --force to overwrite.")
        
    # 4. Update project.json
    project_json_path = root / ".orchestrator" / "project.json"
    if project_json_path.exists():
        try:
            config = read_json(project_json_path)
            config["firebase_distribution"] = True
            config["delivery_provider"] = "firebase"
            config["distribution_script_path"] = "scripts/distribute_ios.sh"
            config["delivery_method"] = method or config.get("delivery_method")

            # App ID resolution
            firebase_plist_path = firebase_plist or config.get("firebase_plist_path")
            if not firebase_plist_path:
                project_name = config.get("project_name") or root.name
                firebase_plist_path = f"{project_name}/GoogleService-Info.plist"

            config["firebase_plist_path"] = firebase_plist_path

            if provisioning_profile_specifier:
                config["provisioning_profile_specifier"] = provisioning_profile_specifier

            if team_id:
                config["development_team"] = team_id

            if asc_key_id: config["asc_key_id"] = asc_key_id
            if asc_issuer_id: config["asc_issuer_id"] = asc_issuer_id
            if asc_key_path: config["asc_key_path"] = asc_key_path

            write_json(project_json_path, config)
            print(f"  ✅ Updated {project_json_path.relative_to(root)}")
        except Exception as e:
            print(f"  ❌ Failed to update project.json: {e}")
    else:
        print(f"  ⚠️  {project_json_path.relative_to(root)} not found. Skipping config update.")

    # 5. Check for GoogleService-Info.plist
    # Reload config to get latest values
    final_config = read_json(project_json_path) if project_json_path.exists() else {}
    gs_info_path = final_config.get("firebase_plist_path") or (root.name + "/GoogleService-Info.plist")

    gs_info = root / gs_info_path
    if not gs_info.exists():
        print(f"\n  \033[93m⚠️  Warning:\033[0m {gs_info_path} not found.")
        print("     You must download this from Firebase Console for distribution to work.")
    else:
        print(f"  ✅ Found {gs_info_path}")
    print("\n✨ Setup complete!")


def detect_identity_info() -> tuple[str | None, str | None]:
    """Detects Team ID and suggests a distribution method based on local identities."""
    try:
        output = subprocess.check_output(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        # Match something like "Apple Development: Name (TEAMID)" or "Apple Distribution: Name (TEAMID)"
        # Prioritize Distribution if we want ad-hoc, but Development is "safer" for first-run
        team_match = re.search(r"\(([A-Z0-9]{10})\)", output)
        team_id = team_match.group(1) if team_match else None
        
        method = "ad-hoc"
        if "Apple Development:" in output and "Apple Distribution:" not in output:
            # If only dev certs found, suggest 'debugging' method to avoid export errors
            method = "debugging"
            
        return team_id, method
    except Exception:
        pass
    return None, None


def detect_provisioning_profile_specifier(root: Path) -> str | None:
    candidates = [
        root / "ai" / "scripts" / "distribute_ios.sh",
        root / "scripts" / "distribute_ios.sh",
    ]
    installed_profiles = installed_provisioning_profile_names()
    pattern = re.compile(r'PROVISIONING_PROFILE_SPECIFIER="([^"]+)"')
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            match = pattern.search(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if match and match.group(1) in installed_profiles:
            return match.group(1)
    return None


def installed_provisioning_profile_names() -> set[str]:
    profiles_dir = Path.home() / "Library" / "MobileDevice" / "Provisioning Profiles"
    if not profiles_dir.exists():
        return set()

    names: set[str] = set()
    for profile_path in profiles_dir.glob("*.mobileprovision"):
        try:
            xml = subprocess.check_output(
                ["/usr/bin/security", "cms", "-D", "-i", str(profile_path)],
                stderr=subprocess.DEVNULL,
            )
            plist = plistlib.loads(xml)
            name = plist.get("Name")
            if isinstance(name, str):
                names.add(name)
        except Exception:
            continue
    return names


def infer_firebase_plist_path(root: Path, project_name: str | None = None) -> str | None:
    root = root.expanduser().resolve()
    matches = []
    for candidate in sorted(root.rglob("GoogleService-Info.plist")):
        if not candidate.is_file():
            continue
        try:
            matches.append(candidate.relative_to(root).as_posix())
        except ValueError:
            continue

    if project_name:
        preferred = [
            match for match in matches
            if match == f"{project_name}/GoogleService-Info.plist" or match.startswith(f"{project_name}/")
        ]
        if preferred:
            return preferred[0]

    if matches:
        return matches[0]

    if project_name:
        return f"{project_name}/GoogleService-Info.plist"
    return "GoogleService-Info.plist"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Setup Firebase distribution scripts and config.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing scripts")
    parser.add_argument("--firebase-plist", help="Path to GoogleService-Info.plist relative to project root")
    parser.add_argument("--provisioning-profile", help="Provisioning profile specifier to use for manual signing")
    parser.add_argument("--team-id", help="Apple Development Team ID")
    parser.add_argument("--method", help="Distribution method (ad-hoc, development, etc.)")
    parser.add_argument("--asc-key-id", help="App Store Connect API Key ID")
    parser.add_argument("--asc-issuer-id", help="App Store Connect API Issuer ID")
    parser.add_argument("--asc-key-path", help="Path to App Store Connect API .p8 key file")
    parser.add_argument("--root", help="Project root directory")
    parser.add_argument("--interactive", action="store_true", help="Run in interactive mode to collect missing info")
    args = parser.parse_args()

    try:
        setup_distribution(
            force=args.force,
            firebase_plist=args.firebase_plist,
            provisioning_profile=args.provisioning_profile,
            team_id=args.team_id,
            method=args.method,
            asc_key_id=args.asc_key_id,
            asc_issuer_id=args.asc_issuer_id,
            asc_key_path=args.asc_key_path,
            root=args.root,
            interactive=args.interactive,
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
