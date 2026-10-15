#!/usr/bin/env bash
# run_weekly_sales_inventory.sh - weekly sales and inventory refresh (spec 9.1, 9.5; D11).
# Cron: Thursday 06:00 UTC. Runs both reports with --weekly-refresh --sink db for every
# marketplace, logs to logs/sales_inventory_<YYYYMMDD>.log, holds a lock so two copies
# never overlap, and retries a failed report up to 3 attempts, 15 minutes apart, the
# same day. Exit code: 0 both reports ok; 2 nothing could run (credentials, database,
# arguments; not retried); 1 any other failure after the last attempt.
# Override for a manual re-run, e.g.:  MPS=UK ./run_weekly_sales_inventory.sh
set -u
cd "$(dirname "$0")" || exit 2
mkdir -p logs

PY=.venv/bin/python
MPS="${MPS:-US,UK,DE,FR,IT,ES}"
TRIGGER="${TRIGGER:-scheduled}"
ATTEMPTS=3
RETRY_WAIT_S=900
STEP_TIMEOUT=3h
LOG="logs/sales_inventory_$(date -u +%Y%m%d).log"

log() { echo "$(date -u -Is) $*" >> "$LOG"; }

exec 9>logs/.sales_inventory.lock
if ! flock -n 9; then
  log "already running (lock held); not started"
  exit 1
fi
if [ ! -x "$PY" ]; then
  log "no virtual environment at $PY (see docs/RUNBOOK.md)"
  exit 2
fi

sales_done=0
inventory_done=0
for attempt in $(seq 1 "$ATTEMPTS"); do
  log "=== attempt $attempt of $ATTEMPTS (marketplaces $MPS)"
  if [ "$sales_done" -eq 0 ]; then
    timeout "$STEP_TIMEOUT" "$PY" sp_api_sales_report.py --weekly-refresh --marketplaces "$MPS" \
      --sink db --trigger "$TRIGGER" >> "$LOG" 2>&1
    rc=$?
    log "sales exit code $rc"
    [ "$rc" -eq 0 ] && sales_done=1
    [ "$rc" -eq 2 ] && { log "sales: nothing could run - not retried"; exit 2; }
  fi
  if [ "$inventory_done" -eq 0 ]; then
    timeout "$STEP_TIMEOUT" "$PY" sp_api_inventory_report.py --weekly-refresh --marketplaces "$MPS" \
      --sink db --trigger "$TRIGGER" >> "$LOG" 2>&1
    rc=$?
    log "inventory exit code $rc"
    [ "$rc" -eq 0 ] && inventory_done=1
    [ "$rc" -eq 2 ] && { log "inventory: nothing could run - not retried"; exit 2; }
  fi
  if [ "$sales_done" -eq 1 ] && [ "$inventory_done" -eq 1 ]; then
    log "=== done: sales and inventory ok"
    exit 0
  fi
  if [ "$attempt" -lt "$ATTEMPTS" ]; then
    log "retrying the failed report(s) in $((RETRY_WAIT_S / 60)) minutes"
    sleep "$RETRY_WAIT_S"
  fi
done
log "=== FAILED after $ATTEMPTS attempts (sales ok=$sales_done, inventory ok=$inventory_done)"
exit 1
