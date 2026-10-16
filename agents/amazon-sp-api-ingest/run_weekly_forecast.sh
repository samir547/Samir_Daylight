#!/usr/bin/env bash
# run_weekly_forecast.sh - weekly Amazon forecast pull, stored as versions (spec 8, 9.5; D5, D12).
# Cron: Thursday 07:00 UTC. One run per week; there is no second scheduled run, and a
# missed week cannot be recovered from Amazon, so a failure is retried up to 3 attempts,
# 15 minutes apart, the same day. "No new version" is exit 0. Logs to
# logs/forecast_<YYYYMMDD>.log; a lock prevents overlapping runs. Raw JSON is archived
# to archive/forecast/ by the script. Exit code: 0 ok; 2 nothing could run (not
# retried); 1 failure after the last attempt.
# Override for a manual re-run, e.g.:  MPS=DE TRIGGER=manual ./run_weekly_forecast.sh
set -u
cd "$(dirname "$0")" || exit 2
mkdir -p logs

PY=.venv/bin/python
MPS="${MPS:-US,UK,DE,FR,IT,ES}"
TRIGGER="${TRIGGER:-scheduled}"
ATTEMPTS=3
RETRY_WAIT_S=900
STEP_TIMEOUT=3h
LOG="logs/forecast_$(date -u +%Y%m%d).log"

log() { echo "$(date -u -Is) $*" >> "$LOG"; }

exec 9>logs/.forecast.lock
if ! flock -n 9; then
  log "already running (lock held); not started"
  exit 1
fi
if [ ! -x "$PY" ]; then
  log "no virtual environment at $PY (see docs/RUNBOOK.md)"
  exit 2
fi

for attempt in $(seq 1 "$ATTEMPTS"); do
  log "=== attempt $attempt of $ATTEMPTS (marketplaces $MPS)"
  timeout "$STEP_TIMEOUT" "$PY" sp_api_forecast_report.py --marketplaces "$MPS" \
    --sink db --trigger "$TRIGGER" >> "$LOG" 2>&1
  rc=$?
  log "forecast exit code $rc"
  if [ "$rc" -eq 0 ]; then
    log "=== done: forecast ok"
    exit 0
  fi
  if [ "$rc" -eq 2 ]; then
    log "forecast: nothing could run - not retried"
    exit 2
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    log "retrying in $((RETRY_WAIT_S / 60)) minutes"
    sleep "$RETRY_WAIT_S"
  fi
done
log "=== FAILED after $ATTEMPTS attempts"
exit 1
