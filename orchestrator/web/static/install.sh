#!/bin/sh
# Set up Orchestrator on this computer and add it to your account, in one step:
#
#   curl -fsSL https://swift-orch-web-20260923.web.app/install.sh | sh
#
# It installs what is missing (Python 3.11+, Git, pipx, cloudflared), installs Orchestrator, shows a code to confirm in
# your browser, and starts Orchestrator at login so the hosted app can always reach this computer.
# Options: --no-service (don't start at login)   --dry-run (only print what it would do)
set -eu

REPO="${ORCHESTRATOR_REPO:-git+https://github.com/leemosupreemo/orchestrator.git}"
SERVICE=1
DRY=0
for arg in "$@"; do
  case "$arg" in
    --no-service) SERVICE=0 ;;
    --dry-run) DRY=1 ;;
    -h|--help) echo "Usage: curl -fsSL <app>/install.sh | sh -s -- [--no-service] [--dry-run]"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '\n==> %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
run() {
  if [ "$DRY" = 1 ]; then printf '    would run: %s\n' "$*"; else "$@"; fi
}
fail() { printf '\nSetup stopped: %s\n' "$1" >&2; exit 1; }

OS="$(uname -s)"
case "$OS" in
  Darwin|Linux) ;;
  *) fail "this installer supports macOS and Linux. On Windows, install Orchestrator from a WSL terminal." ;;
esac

# The newest Python that is 3.11 or later, or nothing.
find_python() {
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if have "$candidate" && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$candidate"; return 0
    fi
  done
  return 1
}

brew_install() {
  have brew || fail "$1 is missing. Install Homebrew from https://brew.sh, then run this again."
  run brew install "$2"
}

say "Checking this computer"
PYTHON="$(find_python || true)"
if [ -z "$PYTHON" ]; then
  if [ "$OS" = Darwin ]; then
    brew_install "Python 3.11 or later" python@3.12
    PYTHON="$(find_python || true)"
  fi
  [ "$DRY" = 1 ] && PYTHON="${PYTHON:-python3}"
  [ -n "$PYTHON" ] || fail "Python 3.11 or later is needed. Install it (https://www.python.org/downloads/), then run this again."
fi
echo "    Python: $PYTHON"

if ! have git; then
  if [ "$OS" = Darwin ]; then
    say "Git is missing"
    run xcode-select --install || true
    fail "finish installing the Command Line Tools in the window that opened, then run this again."
  fi
  fail "Git is missing. Install it with your package manager (for example: sudo apt install git), then run this again."
fi
echo "    Git: $(command -v git)"

if have cloudflared; then
  echo "    cloudflared: $(command -v cloudflared)"
elif [ "$OS" = Darwin ]; then
  say "Installing cloudflared (it makes this computer reachable from the web app)"
  brew_install cloudflared cloudflared
else
  echo "    cloudflared is missing. Install it from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
  echo "    Orchestrator works without it here, but the hosted app can't reach this computer until it is installed."
fi

if ! have pipx; then
  say "Installing pipx"
  if [ "$OS" = Darwin ] && have brew; then
    run brew install pipx
  else
    run "$PYTHON" -m pip install --user pipx
  fi
fi

say "Installing Orchestrator"
PIPX="pipx"
have pipx || PIPX="$PYTHON -m pipx"
run $PIPX install --force --python "$PYTHON" "$REPO"
run $PIPX ensurepath
PATH="$HOME/.local/bin:$PATH"
export PATH

say "Adding this computer to your account"
echo "    A code appears below. Open the link it shows, sign in, and confirm."
run orchestrator connect

if [ "$SERVICE" = 1 ]; then
  say "Starting Orchestrator at login"
  run orchestrator service install
fi

printf '\nDone. Open https://swift-orch-web-20260923.web.app and sign in: this computer appears there.\n'
if [ "$SERVICE" = 0 ]; then
  echo "To start it now, run: orchestrator ui --tunnel"
fi
echo "If \`orchestrator\` isn't found in a new terminal window, open a new one so its PATH updates."
