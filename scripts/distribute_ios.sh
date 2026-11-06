#!/bin/bash
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

if [ -z "$BUILD_NUMBER" ]; then
    BUILD_NUMBER="$(date +%Y%m%d%H%M%S)"
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

echo "🚀 Starting Distribution for $SCHEME (Build: $BUILD_NUMBER)..."

# 1. Archive
echo "📦 Archiving project (setting CURRENT_PROJECT_VERSION=$BUILD_NUMBER)..."

AUTH_FLAGS=""
if [ -n "$ASC_KEY_ID" ] && [ -n "$ASC_ISSUER_ID" ] && [ -n "$ASC_KEY_PATH" ]; then
    AUTH_FLAGS="-authenticationKeyID $ASC_KEY_ID -authenticationKeyIssuerID $ASC_ISSUER_ID -authenticationKeyPath $ASC_KEY_PATH"
fi

DEVELOPMENT_TEAM_FLAG=""
if [ -n "$DEVELOPMENT_TEAM" ]; then
    DEVELOPMENT_TEAM_FLAG="DEVELOPMENT_TEAM=$DEVELOPMENT_TEAM"
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
            $DEVELOPMENT_TEAM_FLAG \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -workspace "$WORKSPACE_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            $DEVELOPMENT_TEAM_FLAG \
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
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            $DEVELOPMENT_TEAM_FLAG \
            $AUTH_FLAGS \
            -allowProvisioningUpdates
    else
        xcodebuild archive \
            -project "$PROJECT_PATH" \
            -scheme "$SCHEME" \
            -configuration "$CONFIGURATION" \
            -archivePath "$ARCHIVE_PATH" \
            -destination "generic/platform=iOS" \
            CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
            $DEVELOPMENT_TEAM_FLAG \
            $AUTH_FLAGS \
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
        $( [ ! -z "$DIST_GROUPS" ] && echo "--groups $DIST_GROUPS" )
else
    echo "❌ Missing GoogleService-Info.plist: $GS_INFO_PATH"
    echo "      - You can specify the path with --firebase-plist"
    exit 1
fi

echo "✅ Distribution Complete!"
