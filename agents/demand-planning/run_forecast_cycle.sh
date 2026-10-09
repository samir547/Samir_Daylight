#!/usr/bin/env bash
# run_forecast_cycle.sh — the ONE thing cron should call (monthly).
#
# Two steps, in this order, and only this order:
#   1. Rebuild dbo.forecast_training_data from dbo.INVOICES_TEMP (whatever that
#      table currently holds — its own refresh is a separate, external process
#      this script does not own or trigger, so schedule this AFTER it finishes).
#   2. Run the forecast against the freshly rebuilt table, rolling
#      TRAIN/TEST/FORECAST forward automatically (--auto-window) and appending
#      to dbo.forecast_output / dbo.forecast_accuracy (--write-db).
#      --require-roll makes step 2 stop with exit 3 — forecasting and writing
#      nothing — if there is no newer complete month than config.TEST_END.
#
# `set -euo pipefail` makes this fail fast: if step 1 fails for any reason
# (after its own internal retries — see database/build_training_data.py),
# step 2 never runs, so a bad cycle never forecasts off a stale or half-built
# table. Nothing here silently swallows an error and "forecasts anyway".
#
# Exit codes: 0 = cycle complete | 1 = a step failed | 2 = bad flag combination |
#             3 = no new complete month to roll onto (nothing written).
# Any non-zero exit is a failed cycle: let cron mail it (MAILTO) or wrap it in
# your monitoring. Every line is also appended to logs/cycle_<date>.log.
#
# Optional environment overrides:
#   VENV_DIR            virtualenv to activate   (default: ./venv)
#   FORECAST_OUTPUT_DIR where this run's CSVs go (default: ./output/runs/<timestamp>)
#
# Example crontab (06:30 on the 5th of each month, after the invoice refresh):
#   MAILTO=you@example.com
#   30 6 5 * *  /opt/prophet_forecast/run_forecast_cycle.sh
set -euo pipefail
cd "$(dirname "$0")"

# ── Logging: everything below goes to the console AND logs/cycle_<date>.log ───
mkdir -p logs
LOG_FILE="logs/cycle_$(date +%F).log"
exec > >(tee -a "$LOG_FILE") 2>&1

trap 'rc=$?; echo "[$(date -Iseconds)] Cycle FAILED (exit $rc) — see $LOG_FILE" >&2' ERR

# ── Python environment ────────────────────────────────────────────────────────
VENV_DIR="${VENV_DIR:-$(pwd)/venv}"
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
else
    echo "[$(date -Iseconds)] WARNING: no virtualenv at $VENV_DIR — using python from PATH." >&2
fi
PYTHON="$(command -v python || command -v python3)"
echo "[$(date -Iseconds)] Using $PYTHON ($("$PYTHON" --version 2>&1))"

# ── Per-run output folder: each cycle keeps its own CSVs ──────────────────────
export FORECAST_OUTPUT_DIR="${FORECAST_OUTPUT_DIR:-$(pwd)/output/runs/$(date +%Y-%m-%d_%H%M%S)}"
mkdir -p "$FORECAST_OUTPUT_DIR"
echo "[$(date -Iseconds)] Outputs → $FORECAST_OUTPUT_DIR"

echo "[$(date -Iseconds)] Rebuilding forecast_training_data…"
"$PYTHON" database/build_training_data.py

echo "[$(date -Iseconds)] Running forecast (--auto-window --require-roll --write-db)…"
"$PYTHON" main.py --auto-window --require-roll --write-db

echo "[$(date -Iseconds)] Cycle complete."
