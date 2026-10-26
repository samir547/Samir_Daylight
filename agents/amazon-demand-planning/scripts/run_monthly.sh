#!/usr/bin/env bash
# run_monthly.sh - the monthly Amazon order forecast for one pool (default US).
#
# What it does: pulls fresh data, builds the forecast, loads it into the database, logs everything.
# Safe to schedule on several days of the month: once a month has loaded successfully it does nothing
# more for that month, and if Amazon's last closed month is not in the database yet it exits quietly
# and the next scheduled day tries again.
#
# Exit code: 0 loaded (or already done this month); 1 failed after the retry; 2 could not run
# (settings, credentials, no virtual environment); 3 data not ready yet; 4 sanity check refused to load.
# Manual run:   POOL=US ./scripts/run_monthly.sh        Dry run (no database write): NO_LOAD=1 ./scripts/run_monthly.sh
set -u
cd "$(dirname "$0")/.." || exit 2
mkdir -p logs

PY=.venv/bin/python
POOL="${POOL:-US}"
MONTH="$(date -u +%Y%m)"
LOG="logs/monthly_${POOL}_${MONTH}.log"
DONE="logs/.done_${POOL}_${MONTH}"
EXTRA=""
[ "${NO_LOAD:-0}" = "1" ] && EXTRA="--no-load"

log() { echo "$(date -u -Is) $*" >> "$LOG"; }

exec 9>logs/.monthly.lock
if ! flock -n 9; then log "already running (lock held); not started"; exit 1; fi
if [ ! -x "$PY" ]; then log "no virtual environment at $PY (see docs/runbook.md)"; exit 2; fi
if [ -f "$DONE" ] && [ "${NO_LOAD:-0}" != "1" ] && [ -z "${FORCE_RERUN:-}" ]; then
  log "$POOL $MONTH already loaded; nothing to do"; exit 0
fi

for attempt in 1 2; do
  log "=== attempt $attempt of 2 (pool $POOL)"
  timeout 2h "$PY" -m order_forecast run-cycle --pool "$POOL" $EXTRA >> "$LOG" 2>&1
  rc=$?
  log "run-cycle exit code $rc"
  case "$rc" in
    0) [ -z "$EXTRA" ] && touch "$DONE"; log "=== done"; exit 0 ;;
    2|3|4) exit "$rc" ;;                       # not worth retrying within the hour
  esac
  [ "$attempt" -lt 2 ] && { log "retrying in 15 minutes"; sleep 900; }
done
log "=== failed after 2 attempts"
exit 1
