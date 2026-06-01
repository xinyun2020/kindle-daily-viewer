#!/usr/bin/env bash
# Install KDV as a macOS LaunchAgent (survives reboot).
# Usage: ./scripts/install-service.sh [--port 8080]
#
# Requires: python3, Full Disk Access granted to Python.app
#   System Settings → Privacy & Security → Full Disk Access → add:
#   /opt/homebrew/Cellar/python@3.14/*/Frameworks/Python.framework/Versions/*/Resources/Python.app
#
# Uninstall: launchctl unload ~/Library/LaunchAgents/com.kdv.server.plist && rm ~/Library/LaunchAgents/com.kdv.server.plist

set -euo pipefail

PORT="${1:-8080}"
if [[ "$1" == "--port" ]]; then PORT="${2:-8080}"; fi

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON3="$(which python3)"
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
