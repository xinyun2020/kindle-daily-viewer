#!/usr/bin/env bash
# Health check watchdog for KDV server.
# Pings localhost:PORT — if no response, restarts the launchd service.
# Designed to run via launchd every 60s.
set -euo pipefail

PORT="${1:-8082}"
PLIST="com.kdv.server"

if ! curl -sf --max-time 5 "http://localhost:$PORT/" > /dev/null 2>&1; then
    echo "$(date): KDV unresponsive on port $PORT — restarting" >> /tmp/kdv-watchdog.log
    launchctl kickstart -k "gui/$(id -u)/$PLIST"
fi
