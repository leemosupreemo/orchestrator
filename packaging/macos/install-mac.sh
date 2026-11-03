#!/bin/sh
# Install Orchestrator on a Mac you reach over SSH, such as a Mac mini in a closet, and add it to your account.
#
# Use the command from Add a Mac in the web app; it passes a one-time token and your project:
#   curl -fsSL https://<hosted>/install-mac.sh | sh -s -- --token enroll_... --project git@github.com:you/app.git
#
# It downloads the signed app for this Mac's chip, checks it against the published checksum and Apple's signature,
# copies it to Applications and runs `orchestrator enroll` with your arguments. An app that's already installed is
# kept as it is (it updates itself). Run it as the Mac user Orchestrator should work as, logged in on the Mac's screen.
set -eu

RELEASES="${ORCHESTRATOR_RELEASES_URL:-https://swift-orch-web-20260923.web.app/releases/macos.json}"
APPLICATIONS="${ORCHESTRATOR_APPLICATIONS:-/Applications}"

say() { printf '  %s\n' "$1"; }
fail() { printf '  ✗ %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "This installs the Mac app. On other systems, install Orchestrator with pipx."
[ "$(id -u)" != 0 ] || fail "Run this as the Mac user Orchestrator should work as, not as root (no sudo)."
user="$(id -un)"
[ "$(stat -f %Su /dev/console)" = "$user" ] || fail "$user isn't logged in on this Mac's screen. Orchestrator runs in that login session, so turn on automatic login for $user (System Settings › Users & Groups), restart, then run this again."
case "$(uname -m)" in
  arm64) arch=arm64 ;;
  x86_64) arch=x86_64 ;;
  *) fail "This Mac's chip ($(uname -m)) isn't supported." ;;
esac

work="$(mktemp -d "${TMPDIR:-/tmp}/orchestrator-install.XXXXXX")"
mount="$work/mount"
cleanup() {
  if [ -d "$mount" ]; then hdiutil detach -quiet "$mount" >/dev/null 2>&1 || true; fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 1' INT TERM

installed="$APPLICATIONS/Orchestrator.app"
if [ ! -d "$installed" ] && { [ -d "$HOME/Applications/Orchestrator.app" ] || [ ! -w "$APPLICATIONS" ]; }; then
  installed="$HOME/Applications/Orchestrator.app"
fi
if [ -d "$installed" ]; then
  say "✓ Orchestrator is already installed at $installed; using it (it updates itself)."
else
  curl -fsSL --proto =https -o "$work/release.json" "$RELEASES" || fail "Couldn't read the release list at $RELEASES."
  url="$(plutil -extract "$arch.url" raw -o - "$work/release.json" 2>/dev/null)" || fail "There's no published download for $arch Macs yet."
  sum="$(plutil -extract "$arch.sha256" raw -o - "$work/release.json" 2>/dev/null)" || fail "The release list has no checksum for $arch."
  version="$(plutil -extract version raw -o - "$work/release.json" 2>/dev/null)" || version="unknown"
  case "$url" in https://*) ;; *) fail "The release list points somewhere other than an https:// address; nothing was installed." ;; esac
  say "Downloading Orchestrator $version for $arch…"
  curl -fsSL --proto =https -o "$work/Orchestrator.dmg" "$url" || fail "Couldn't download $url."
  actual="$(shasum -a 256 "$work/Orchestrator.dmg" | cut -d ' ' -f 1)"
  [ "$actual" = "$sum" ] || fail "The download doesn't match the published checksum; nothing was installed."
  mkdir "$mount"
  hdiutil attach -readonly -nobrowse -quiet -mountpoint "$mount" "$work/Orchestrator.dmg" || fail "Couldn't open the downloaded disk image."
  app="$mount/Orchestrator.app"
  codesign --verify --deep --strict "$app" >/dev/null 2>&1 || fail "The app's signature doesn't check out; nothing was installed."
  spctl --assess --type execute "$app" >/dev/null 2>&1 || fail "macOS doesn't trust this copy of the app (not notarized); nothing was installed."
  mkdir -p "$(dirname "$installed")"
  ditto "$app" "$installed" || fail "Couldn't copy Orchestrator to $(dirname "$installed")."
  hdiutil detach -quiet "$mount" >/dev/null 2>&1 || true
  say "✓ Installed Orchestrator $version in $(dirname "$installed")"
fi

status=0
"$installed/Contents/Resources/runtime/bin/orchestrator" enroll "$@" || status=$?
exit "$status"
