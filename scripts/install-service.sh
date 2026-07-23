#!/usr/bin/env bash
# Install KDV as a macOS LaunchAgent (survives reboot).
# Usage: ./scripts/install-service.sh [--port 8080]
#
# Requires: python3 with Full Disk Access (KDV reads the vault under ~/Documents,
#   which is TCC-protected). Grant FDA to the Python.app, then `brew pin python@3.14`
#   so a later upgrade can't swap the binary out from under the grant.
#   System Settings → Privacy & Security → Full Disk Access → add:
#   /opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework/Versions/*/Resources/Python.app
#
# Uninstall: launchctl unload ~/Library/LaunchAgents/com.kdv.server.plist && rm ~/Library/LaunchAgents/com.kdv.server.plist

set -euo pipefail

PORT="${1:-8080}"
if [[ "$1" == "--port" ]]; then PORT="${2:-8080}"; fi

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# WHY THIS MATTERS (KDV broke 2026-06-20): KDV reads the Obsidian vault under
# ~/Documents, which is TCC-protected — so the python binary running KDV needs
# Full Disk Access. FDA is granted to a SPECIFIC binary. Two traps:
#   1. Homebrew's /opt/homebrew/bin/python3 → versioned binary that brew SWAPS on
#      every `brew upgrade python@3.14` (3.14.5→3.14.6). The new binary has NO FDA
#      grant, so reads fail "Operation not permitted" under launchd → server dies.
#   2. Apple's /usr/bin/python3 resolves to CommandLineTools python, which ALSO has
#      no FDA by default — so it is NOT a safe fallback.
# Robust fix: use the Homebrew python AND pin it (`brew pin python@3.14`) so brew
# stops swapping it, AND grant that Python.app FDA once. Pinning is the real
# prevention — see scripts/preflight-check.sh and the brew-upgrade guard.
PYTHON3="${KDV_PYTHON:-$(which python3)}"
PLIST_SRC="$SCRIPT_DIR/com.kdv.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.kdv.server.plist"

if [[ ! -f "$SCRIPT_DIR/kindle_daily_viewer.py" ]]; then
    echo "Error: kindle_daily_viewer.py not found in $SCRIPT_DIR"
    exit 1
fi

# Generate plist from template
sed -e "s|__PYTHON3__|$PYTHON3|" \
    -e "s|__KDV_SCRIPT__|$SCRIPT_DIR/kindle_daily_viewer.py|" \
    "$PLIST_SRC" > "$PLIST_DST"

# Add --port if non-default
if [[ "$PORT" != "8080" ]]; then
    sed -i '' "s|</array>|        <string>--port</string>\n        <string>$PORT</string>\n    </array>|" "$PLIST_DST"
fi

# Unload existing if present
launchctl unload "$PLIST_DST" 2>/dev/null || true

# Ensure Python is allowed through macOS firewall (survives reboot)
PYTHON_APP="$(dirname "$(dirname "$PYTHON3")")/Resources/Python.app"
if [[ -d "$PYTHON_APP" ]]; then
    sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add "$PYTHON_APP" --unblockapp "$PYTHON_APP" 2>/dev/null && \
        echo "Firewall: allowed $PYTHON_APP" || \
        echo "Warning: could not add Python to firewall (run with sudo or allow manually)"
fi

# Load
launchctl load "$PLIST_DST"
sleep 2

if lsof -i ":$PORT" -P 2>/dev/null | grep -q LISTEN; then
    echo "KDV running on port $PORT"
    echo "Plist installed: $PLIST_DST"
else
    echo "Warning: service loaded but not listening on port $PORT"
    echo "Check /tmp/kdv.err for errors"
    echo "Most common issue: python3 needs Full Disk Access"
fi

# --- Watchdog (restarts the server if it stops responding) ---
# WHY the staged copy: launchd cannot exec/read a script inside TCC-protected
# ~/Documents unless the executing binary has Full Disk Access. The watchdog
# needs no vault access (curls localhost + kickstarts), so we stage a copy in
# the non-TCC ~/.local/bin and run it via /bin/bash rather than granting FDA.
# Pointing the plist at the repo copy fails with exit 126 "Operation not
# permitted" (the 2026-06-10 breakage that ran silently until 2026-07-06).
WATCHDOG_SRC="$SCRIPT_DIR/scripts/watchdog.sh"
WATCHDOG_STAGED="$HOME/.local/bin/kdv-watchdog.sh"
WATCHDOG_PLIST_SRC="$SCRIPT_DIR/com.kdv.watchdog.plist"
WATCHDOG_PLIST_DST="$HOME/Library/LaunchAgents/com.kdv.watchdog.plist"

if [[ -f "$WATCHDOG_SRC" && -f "$WATCHDOG_PLIST_SRC" ]]; then
    mkdir -p "$HOME/.local/bin"
    cp "$WATCHDOG_SRC" "$WATCHDOG_STAGED"
    chmod +x "$WATCHDOG_STAGED"
    xattr -c "$WATCHDOG_STAGED" 2>/dev/null || true

    sed -e "s|__WATCHDOG_SCRIPT__|$WATCHDOG_STAGED|" \
        -e "s|__PORT__|$PORT|" \
        "$WATCHDOG_PLIST_SRC" > "$WATCHDOG_PLIST_DST"

    launchctl unload "$WATCHDOG_PLIST_DST" 2>/dev/null || true
    launchctl load "$WATCHDOG_PLIST_DST"
    sleep 1
    WD_STATUS="$(launchctl list | awk '$3=="com.kdv.watchdog"{print $2}')"
    if [[ "$WD_STATUS" == "0" ]]; then
        echo "Watchdog installed and running (status 0)"
    else
        echo "Warning: watchdog loaded but last-exit status is ${WD_STATUS:-unknown}"
        echo "Check /tmp/kdv-watchdog.log"
    fi
fi
