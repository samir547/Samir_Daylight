"""
Build/refresh [dbo].[forecast_training_data] from raw invoices.

Runs database/forecast_training_data.sql against Azure SQL (drops and rebuilds
the staging table), then writes an audit log of any (Item_No, BDM) pairs that
carried more than one Region in dbo.BDM and therefore had to be collapsed to a
single region by the build's MIN(Region) tie-break.

The collapse is what prevents the region join from fanning out invoice rows (see
the header comment in forecast_training_data.sql). The audit log keeps that
resolution visible even though the rebuilt table looks clean: the chosen region
is a *resolved ambiguity*, not a verified-correct answer, and may need business
confirmation before the stored `region` column is used for region-level
reporting.

Usage:
    python database/build_training_data.py          # rebuild + write audit log
    python database/build_training_data.py --audit-only   # only (re)write the log
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Reuse the pipeline's existing engine builder — no new connection path.
# _get_engine() itself does NOT retry; the retry/backoff below is local to
# rebuild_table(), mirroring data.loader._read_sql()'s policy (same constants)
# rather than importing it, since that name is loader-private.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
from data.loader import _get_engine  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("build_training_data")

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 5

SQL_PATH = Path(__file__).resolve().parent / "forecast_training_data.sql"
AUDIT_PATH = config.OUTPUT_DIR / "region_ambiguity_log.csv"

# Mirrors the MIN(Region) tie-break used inside forecast_training_data.sql, and
# surfaces every pair that had to be collapsed (n_regions > 1).
AMBIGUITY_QUERY = """
SELECT
    Item_No                     AS item_no,
    BDM                         AS bdm,
    COUNT(DISTINCT Region)      AS n_regions,
    STRING_AGG(Region, ' | ') WITHIN GROUP (ORDER BY Region) AS all_regions,
    MIN(Region)                 AS region_chosen
FROM (SELECT DISTINCT Item_No, BDM, Region FROM [dbo].[BDM] WHERE Region IS NOT NULL) d
GROUP BY Item_No, BDM
HAVING COUNT(DISTINCT Region) > 1
ORDER BY BDM, Item_No
"""


def rebuild_table() -> None:
    """Execute the DROP + SELECT INTO batch that rebuilds forecast_training_data.

    Retries transient connection failures the same way data.loader._read_sql()
    does for ordinary reads. This call is now the first step of an unattended,
    scheduled chain (run_forecast_cycle.sh) — a connection blip here should not
    fail the whole two-month cycle any more readily than a normal data pull
    would. The DROP + SELECT INTO run inside one transaction (engine.begin()),
    so a failed/retried attempt rolls back cleanly and never leaves the table
    half-dropped; each retry starts from the same known state as the last.
    """
    sql = SQL_PATH.read_text(encoding="utf-8")
    logger.info("Rebuilding [dbo].[forecast_training_data] from %s …", SQL_PATH.name)
    engine = _get_engine()

    last_err: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(sql)
            with engine.connect() as conn:
                n = conn.execute(
                    text("SELECT COUNT(*) FROM [dbo].[forecast_training_data]")
                ).scalar_one()
            logger.info("Rebuilt forecast_training_data — %s rows", f"{n:,}")
            return
        except OperationalError as err:
            last_err = err
            wait = _RETRY_BACKOFF_SECONDS * attempt
            logger.warning(
                "Rebuild failed (attempt %d/%d): %s — retrying in %ds",
                attempt, _MAX_RETRIES, err, wait,
            )
            time.sleep(wait)
    raise RuntimeError(
        f"Failed to rebuild forecast_training_data after {_MAX_RETRIES} attempts"
    ) from last_err


def write_audit_log() -> pd.DataFrame:
    """Write the region-ambiguity audit CSV; return the frame for logging."""
    engine = _get_engine()
    with engine.connect() as conn:
        amb = pd.read_sql(text(AMBIGUITY_QUERY), conn)
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    amb.to_csv(AUDIT_PATH, index=False)
    if amb.empty:
        logger.info("No multi-region (Item_No, BDM) pairs found — audit log is empty.")
    else:
        logger.warning(
            "%s (Item_No, BDM) pair(s) had >1 region and were collapsed. "
            "Chosen region is a resolved ambiguity, not a verified answer — "
            "see %s",
            len(amb), AUDIT_PATH,
        )
        for _, r in amb.iterrows():
            logger.warning(
                "  %-10s %-5s  regions=[%s]  chosen=%s",
                r["item_no"], r["bdm"], r["all_regions"], r["region_chosen"],
            )
    logger.info("Region ambiguity log → %s (%s rows)", AUDIT_PATH, len(amb))
    return amb


def main() -> int:
    ap = argparse.ArgumentParser(description="Rebuild forecast_training_data + audit log")
    ap.add_argument("--audit-only", action="store_true",
                    help="Skip the table rebuild; only (re)write the ambiguity log")
    args = ap.parse_args()

    if not args.audit_only:
        rebuild_table()
    write_audit_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
