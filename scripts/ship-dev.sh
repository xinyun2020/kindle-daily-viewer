#!/bin/bash
# Run tests, scan the diff for personal-info leaks, push to origin/dev, then
# reload the local dogfood server so the change is actually live (Python
# doesn't hot-reload — a restart is part of "done", not optional).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== tests =="
python3 -m pytest test_kindle_daily_viewer.py -q

echo "== leak scan (diff vs origin/dev) =="
LEAK=$(git diff origin/dev HEAD | grep -iE '^\+' | grep -viE '^\+\+\+' \
  | grep -iE 'alice|azhang|xinyun2020|personal|/Users/[a-z]' || true)
if [ -n "$LEAK" ]; then
  echo "BLOCKED - possible personal-info leak in diff:"
  echo "$LEAK"
  exit 1
fi

echo "== push =="
git push origin dev

echo "== reload live dogfood server (com.kdv.server) =="
if launchctl list | grep -q com.kdv.server; then
  launchctl kickstart -k "gui/$(id -u)/com.kdv.server"
  sleep 1
  curl -s -o /dev/null -w "reload check: HTTP %{http_code}\n" http://localhost:8082/
else
  echo "com.kdv.server not loaded - skipping reload (nothing running to refresh)"
fi
