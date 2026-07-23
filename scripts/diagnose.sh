#!/usr/bin/env bash
# diagnose.sh — KDV health + root-cause check. Run THIS before guessing why KDV is down.
# WHY: KDV failure is almost always TCC/FDA (the python binary running it lost Full Disk
# Access — usually after `brew upgrade python`). This script checks the known causes in
# order so we stop re-deriving the wrong one (launchd/port have been wrong 4x; it's TCC).
set -uo pipefail
PORT="${1:-8082}"
echo "=== KDV diagnose (port $PORT) ==="
# 1. Is it serving?
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:$PORT/login" 2>/dev/null)
echo "1. HTTP health: $code $([ "$code" = "200" ] && echo '✓ UP — KDV is fine' || echo '✗ down, checking causes')"
[ "$code" = "200" ] && exit 0
# 2. TCC/FDA — the #1 cause. Check the err log for the signature.
echo "2. TCC/Full Disk Access (most common cause):"
if grep -qi "operation not permitted\|errno 1" /tmp/kdv.err 2>/dev/null; then
  echo "   ✗ FDA MISSING — the python binary can't read ~/Documents (TCC-protected)."
  echo "   FIX: System Settings → Privacy & Security → Full Disk Access → add:"
  ls -d /opt/homebrew/Cellar/python@*/*/Frameworks/Python.framework/Versions/*/Resources/Python.app 2>/dev/null | tail -1
else
  echo "   (no FDA error in /tmp/kdv.err)"
fi
# 3. brew swapped python? (breaks the FDA grant)
echo "3. Homebrew python pin status (unpinned = brew swaps it, breaking FDA):"
brew list --pinned 2>/dev/null | grep -q python@3.14 && echo "   ✓ pinned" || echo "   ✗ NOT pinned — run: brew pin python@3.14"
# 4. Port conflict (rarely the cause, checked last on purpose)
echo "4. Port $PORT listener:"
lsof -nP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null | tail -1 || echo "   nothing listening"
echo ""
echo "MOST LIKELY: TCC/FDA (step 2). Fix that before touching launchd or the port."
