#!/usr/bin/env bash
# MIZAN watchdog - run every minute by mizan-watchdog.timer, as root.
#
#   every minute    GET /api/health (answered by the HTTP threads, so it stays
#                   fast even while a long check is running)
#   every 10 min    one real check through the engine (the 49:13 AI probe):
#                   proves the single engine worker is not wedged, and records
#                   whether the AI tier is "up" or "degraded" in
#                   /var/lib/mizan-status/ai.json (served at /_ops/ai.json for
#                   the external keyword monitor)
#
# Restarts mizan after 3 consecutive health failures (3 min unresponsive) or
# 2 consecutive engine failures (20 min). A DEGRADED AI tier does not trigger
# a restart: the deterministic path is still answering correctly, and a
# restart would not bring back a missing model. Systemd's Restart=always
# already handles a process that exits.
set -u
PY=/opt/mizan/venv/bin/python
PROBE=/opt/mizan/app/deploy/probe.py
STATUS=/var/lib/mizan-status/ai.json
STATE=/run/mizan-watchdog
# shellcheck disable=SC1091
. /etc/mizan/mizan.env 2>/dev/null || true
BASE="http://${MIZAN_HOST:-127.0.0.1}:${MIZAN_PORT:-8000}"
mkdir -p "$STATE"

# Only judge a fully started service; while prewarm runs it is "activating".
[ "$(systemctl is-active mizan)" = "active" ] || exit 0

hf=$(cat "$STATE/health_fails" 2>/dev/null || echo 0)
ef=$(cat "$STATE/engine_fails" 2>/dev/null || echo 0)
deep=()
if [ $((10#$(date +%M) % 10)) -eq 0 ]; then deep=(--ai); fi

"$PY" "$PROBE" check --base "$BASE" "${deep[@]}" --status-file "$STATUS"
rc=$?
case $rc in
  0) hf=0; if [ ${#deep[@]} -gt 0 ]; then ef=0; fi ;;
  1) hf=$((hf + 1)) ;;
  2) hf=0; ef=$((ef + 1)) ;;
  *) hf=$((hf + 1)) ;;
esac
echo "$hf" > "$STATE/health_fails"
echo "$ef" > "$STATE/engine_fails"

if [ "$hf" -ge 3 ] || [ "$ef" -ge 2 ]; then
  logger -t mizan-watchdog "restarting mizan (health_fails=$hf engine_fails=$ef)"
  echo 0 > "$STATE/health_fails"
  echo 0 > "$STATE/engine_fails"
  systemctl restart mizan
fi
exit 0
