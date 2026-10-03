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
DIST_GROUPS=""
BUILD_NUMBER=""

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
        --groups) DIST_GROUPS="$2"; shift ;;
        --build-number) BUILD_NUMBER="$2"; shift ;;
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

if [ -z "$BUILD_NUMBER" ]; then
    if [ -n "$WORKSPACE_PATH" ]; then
        CURRENT_BUILD=$(xcodebuild -workspace "$WORKSPACE_PATH" -scheme "$SCHEME" -configuration "$CONFIGURATION" -showBuildSettings 2>/dev/null | awk '/CURRENT_PROJECT_VERSION =/{print $3; exit}')
    else
        CURRENT_BUILD=$(xcodebuild -project "$PROJECT_PATH" -scheme "$SCHEME" -configuration "$CONFIGURATION" -showBuildSettings 2>/dev/null | awk '/CURRENT_PROJECT_VERSION =/{print $3; exit}')
    fi
    if [[ ! "$CURRENT_BUILD" =~ ^[0-9]+$ ]]; then
        echo "Error: Could not determine a numeric CURRENT_PROJECT_VERSION. Pass --build-number explicitly."
        exit 1
    fi
    BUILD_NUMBER=$((CURRENT_BUILD + 1))
fi

RUN_ID="$(date +%Y%m%d-%H%M%S)-$$"
OUTPUT_ROOT="${ORCHESTRATOR_DISTRIBUTION_DIR:-$PWD/build/distribution/$RUN_ID}"
ARCHIVE_PATH="${OUTPUT_ROOT}/${SCHEME}.xcarchive"
EXPORT_PATH="${OUTPUT_ROOT}/export"
IPA_PATH="${EXPORT_PATH}/${SCHEME}.ipa"
mkdir -p "$OUTPUT_ROOT"

if [ -n "${KEYCHAIN_PASSWORD:-}" ]; then
    security unlock-keychain -p "$KEYCHAIN_PASSWORD" ~/Library/Keychains/login.keychain-db >/dev/null 2>&1 || true
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" ~/Library/Keychains/login.keychain-db >/dev/null 2>&1 || true
fi

echo "🚀 Starting Distribution for $SCHEME (Build: $BUILD_NUMBER)..."

# 1. Archive
echo "📦 Archiving project (setting CURRENT_PROJECT_VERSION=$BUILD_NUMBER)..."

AUTH_FLAGS=()
if [ -n "$ASC_KEY_ID" ] && [ -n "$ASC_ISSUER_ID" ] && [ -n "$ASC_KEY_PATH" ]; then
    AUTH_FLAGS=(-authenticationKeyID "$ASC_KEY_ID" -authenticationKeyIssuerID "$ASC_ISSUER_ID" -authenticationKeyPath "$ASC_KEY_PATH")
fi

DEVELOPMENT_TEAM_FLAG=()
if [ -n "$DEVELOPMENT_TEAM" ]; then
    DEVELOPMENT_TEAM_FLAG=("DEVELOPMENT_TEAM=$DEVELOPMENT_TEAM")
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
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            "${DEVELOPMENT_TEAM_FLAG[@]}" \
            "${AUTH_FLAGS[@]}" \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -workspace "$WORKSPACE_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            "${DEVELOPMENT_TEAM_FLAG[@]}" \
            "${AUTH_FLAGS[@]}" \
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
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            "${DEVELOPMENT_TEAM_FLAG[@]}" \
            "${AUTH_FLAGS[@]}" \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -project "$PROJECT_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            "${DEVELOPMENT_TEAM_FLAG[@]}" \
            "${AUTH_FLAGS[@]}" \
            -allowProvisioningUpdates
    fi
fi

# Ensure archive metadata reflects build number
if [ -f "$ARCHIVE_PATH/Info.plist" ]; then
    /usr/libexec/PlistBuddy -c "Set :ApplicationProperties:CFBundleVersion $BUILD_NUMBER" "$ARCHIVE_PATH/Info.plist" 2>/dev/null || true
fi

# 2. Export IPA
echo "📤 Exporting IPA..."
xcodebuild -exportArchive \
    -archivePath "$ARCHIVE_PATH" \
    -exportOptionsPlist "scripts/ExportOptions.plist" \
    -exportPath "$EXPORT_PATH" \
    "${AUTH_FLAGS[@]}" \
    -allowProvisioningUpdates

IPA_PATH=$(find "$EXPORT_PATH" -maxdepth 1 -type f -name '*.ipa' -print -quit)
if [ -z "$IPA_PATH" ]; then
    echo "Error: Xcode export completed without producing an IPA in $EXPORT_PATH"
    exit 1
fi

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
    if [ -z "$TESTERS" ] && [ -z "$DIST_GROUPS" ]; then DIST_GROUPS="internal-testers"; fi
    RECIPIENT_FLAGS=()
    if [ -n "$TESTERS" ]; then RECIPIENT_FLAGS+=(--testers "$TESTERS"); fi
    if [ -n "$DIST_GROUPS" ]; then RECIPIENT_FLAGS+=(--groups "$DIST_GROUPS"); fi
    firebase appdistribution:distribute "$IPA_PATH" \
        --app "$APP_ID" \
        --release-notes "$RELEASE_NOTES" \
        "${RECIPIENT_FLAGS[@]}"
else
    echo "❌ Missing GoogleService-Info.plist: $GS_INFO_PATH"
    echo "      - You can specify the path with --firebase-plist"
    exit 1
fi

echo "✅ Distribution Complete!"
echo "IPA: $IPA_PATH"
echo "Build: $BUILD_NUMBER"
echo "Recipients: testers=${TESTERS:-<none>} groups=${DIST_GROUPS:-<none>}"
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

def secure_asc_key_if_needed(key_path_str: str, root: Path) -> str:
    if not key_path_str:
        return key_path_str
    
    # Resolve input key path
    p = Path(key_path_str).expanduser()
    if not p.is_absolute():
        p = (root / p).resolve()
    else:
        p = p.resolve()
        
    if not p.exists():
        return key_path_str
        
    home = Path.home().resolve()
    secure_paths = [
        home / ".private_keys",
        home / ".appstoreconnect" / "private_keys"
    ]
    
    is_secure_dir = any(p.parent == sp.resolve() for sp in secure_paths)
    
    if is_secure_dir:
        # Already in a secure directory, but let's check/fix permissions
        try:
            mode = p.stat().st_mode
            if (mode & 0o077) != 0:
                print(f"  🔒 Securing permissions for existing key file to 600...")
                p.chmod(0o600)
        except Exception as e:
            print(f"  ⚠️  Failed to set permissions on {p}: {e}")
        return str(p)
        
    # Not in a secure directory! Let's copy it to ~/.private_keys/
    target_dir = home / ".private_keys"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / p.name
        
        print(f"  📦 xcodebuild security constraint: copying .p8 key file to secure directory ~/.private_keys/")
        import shutil
        shutil.copy2(p, target_path)
        target_path.chmod(0o600)
        print(f"  ✅ Copied key to {target_path} and set permissions (600)")
        return str(target_path)
    except Exception as e:
        print(f"  ❌ Error copying API key to secure directory: {e}")
        return key_path_str


def setup_distribution(force=False, firebase_plist=None, provisioning_profile=None, team_id=None, method=None, asc_key_id=None, asc_issuer_id=None, asc_key_path=None, root=None):
    if root is None:
        from orchestrator.project_config import find_project_root
        root = find_project_root()

    root = Path(os.fspath(root)).resolve()
    print(f"🛠️  Setting up distribution for {root.name}...")
    provisioning_profile_specifier = provisioning_profile or detect_provisioning_profile_specifier(root)

    detected_team_id, detected_method = detect_identity_info()

    # We use a helper to get config values safely without relying on a global PROJECT_CONFIG 
    # which might be bound to a different root during tests
    def get_conf_val(key, default=None):
        project_json = root / ".orchestrator" / "project.json"
        if project_json.exists():
            try:
                return read_json(project_json).get(key, default)
            except: pass
        return default

    team_id = team_id or get_conf_val("development_team") or detected_team_id
    method = method or get_conf_val("delivery_method") or detected_method or "ad-hoc"

    if team_id:
        print(f"  ✅ Using Team ID: {team_id}")
    if method:
        print(f"  ✅ Using Method:  {method}")

    asc_key_id = asc_key_id or get_conf_val("asc_key_id")
    asc_issuer_id = asc_issuer_id or get_conf_val("asc_issuer_id")
    asc_key_path = asc_key_path or get_conf_val("asc_key_path")
    if asc_key_path:
        asc_key_path = secure_asc_key_if_needed(asc_key_path, root)

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
            if not config.get("firebase_testers") and not config.get("firebase_groups"):
                config["firebase_groups"] = "internal-testers"

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
    home = Path.home()
    profile_dirs = [
        home / "Library" / "Developer" / "Xcode" / "UserData" / "Provisioning Profiles",
        home / "Library" / "MobileDevice" / "Provisioning Profiles",
    ]

    names: set[str] = set()
    for profiles_dir in profile_dirs:
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
        )

    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
