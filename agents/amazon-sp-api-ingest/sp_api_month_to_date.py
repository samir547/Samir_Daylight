#!/usr/bin/env python3
"""
Fetch the current month to date (sales + inventory) for every marketplace
with credentials, and upsert it into SQL Server - one run does both.

    python sp_api_month_to_date.py                       # all marketplaces with credentials
    python sp_api_month_to_date.py --marketplaces US     # just one
    python sp_api_month_to_date.py --dry-run             # fetch + show, no DB writes

Run it as often as you like (e.g. daily): it never creates duplicates.

How it works
------------
Amazon refuses MONTH-period vendor reports for a month that hasn't closed
(processing status FATAL), so the current month is built from DAY-period
reports and rolled up to one row per ASIN, covering the 1st of the month
through the latest day Amazon has published:

  * Sales: every field is the sum of the days.
  * Inventory: net_received_* is the sum of the days; stock levels
    (sellable, unsellable, aged 90+, open PO) are the last day's values.
    sell_through_rate and unhealthy_* are not provided in daily reports, so
    they stay NULL until the month closes.

Checked against September 2026 (US): these rollups reproduce Amazon's own
monthly figures exactly, except that a stock field the last day leaves
blank is 0 in the monthly report.

Amazon publishes a day ~3 days after it ends (probed 2026-10-11: data
through 10-08 was available, 10-09 was not). The script asks for
today - LAG_DAYS and steps back a day at a time if that isn't ready yet.
DAY-period requests are capped at 15 days, so a month is fetched in up to
three pieces.

The previous month is completed too. Until its final MONTH report exists
in the database, each run requests it; once Amazon serves it (~72h after
month end) it replaces the month-to-date row with the final figures. Before
then the run refreshes it from daily data instead.

No duplicates: the tables hold one row per (marketplace, asin, month)
(see ingest_to_db.py), so each run updates the month's rows in place and
moves end_date forward. period_status is 'month_to_date' until the month's
final figures land, then 'final'.

Marketplaces without credentials in .env are skipped with a message.
A marketplace/month whose fetch fails part-way is not loaded at all, so a
partial fetch never overwrites better data already in the table.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

import ingest_to_db as db
import sp_api_inventory_report as inv
import sp_api_sales_report as spapi

LAG_DAYS = 3            # Amazon publishes a day ~3 days after it ends
MAX_STEP_BACK_DAYS = 3  # how much further back to try if today - LAG_DAYS isn't ready
DAY_WINDOW_MAX = 15     # Amazon's cap on DAY-period request length
REPORT_TIMEOUT_S = 600
NETWORK_RETRIES = 4     # attempts per Amazon call on DNS/connection failures
OUT_DIR = Path(__file__).parent / "output" / "month_to_date"

KINDS = {
    "sales": {
        "report_type": "GET_VENDOR_SALES_REPORT",
        "flatten": spapi.flatten_report,
        "db": db.REPORTS["sales"],
        "sum_cols": None,  # None = every numeric column is summed
    },
    "inventory": {
        "report_type": "GET_VENDOR_INVENTORY_REPORT",
        "flatten": inv.flatten_inventory_report,
        "db": db.REPORTS["inventory"],
        "sum_cols": ["net_received_units", "net_received_cost"],  # the rest: last day's value
    },
}


class PartialFetch(Exception):
    """A chunk other than the newest one failed - the month would be incomplete."""


def iso_start(d: date) -> str:
    return f"{d:%Y-%m-%d}T00:00:00Z"


def iso_end(d: date) -> str:
    return f"{d:%Y-%m-%d}T23:59:59Z"


def month_end(d: date) -> date:
    return (pd.Timestamp(d) + pd.offsets.MonthEnd(0)).date()


def with_network_retries(fn, *args, **kwargs):
    """Retry fn on transient network failures (DNS, dropped connections),
    which do happen on this machine's connection; Amazon's own answers
    (HTTP errors, FATAL reports) are not retried here."""
    for attempt in range(1, NETWORK_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == NETWORK_RETRIES:
                raise
            wait = 30 * attempt
            print(f"    network error ({type(e).__name__}); retrying in {wait}s "
                  f"[{attempt}/{NETWORK_RETRIES - 1}]")
            time.sleep(wait)


def _request(ctx: dict, report_type: str, period: str, start: date, end: date) -> dict:
    options = {"reportPeriod": period, "distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
    report_id = spapi.create_report(ctx["host"], ctx["token"], ctx["marketplace_id"],
                                    iso_start(start), iso_end(end),
                                    report_type=report_type, report_options=options)
    doc_id = spapi.poll_report(ctx["host"], ctx["token"], report_id, timeout_s=REPORT_TIMEOUT_S)
    return spapi.download_report(ctx["host"], ctx["token"], doc_id)


def request(ctx: dict, report_type: str, period: str, start: date, end: date) -> dict:
    return with_network_retries(_request, ctx, report_type, period, start, end)


def fetch_days(ctx: dict, kind: dict, start: date, end: date):
    """Daily rows for start..end, in <=15-day requests. If the newest chunk
    isn't published yet, step its end back a day at a time. Returns (rows,
    last day actually fetched), or ([], None) if no day is available."""
    rows = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=DAY_WINDOW_MAX - 1), end)
        is_last = chunk_end == end
        for _ in range(MAX_STEP_BACK_DAYS + 1):
            try:
                report = request(ctx, kind["report_type"], "DAY", chunk_start, chunk_end)
                break
            except (RuntimeError, TimeoutError, requests.HTTPError) as e:
                if not is_last:
                    raise PartialFetch(f"{chunk_start}..{chunk_end}: {e}") from e
                print(f"    {chunk_start}..{chunk_end} not available yet ({e}); trying one day earlier")
                chunk_end -= timedelta(days=1)
                if chunk_end < chunk_start:
                    break
        else:
            chunk_end = chunk_start - timedelta(days=1)
        if chunk_end < chunk_start:  # nothing in this chunk is published yet
            last = chunk_start - timedelta(days=1)
            return (rows, last) if last >= start else ([], None)
        rows += kind["flatten"](report, ctx["mp"])
        print(f"    days {chunk_start}..{chunk_end}: {len(rows):,} daily rows so far")
        if not is_last:
            chunk_start = chunk_end + timedelta(days=1)
        else:
            return rows, chunk_end
    return rows, end


def roll_up(daily: list, kind: dict, month_start: date) -> pd.DataFrame:
    """Daily rows -> one row per ASIN (incl. the 'ALL' total) for the month so far."""
    cols = list(kind["db"]["columns"])
    num = [c for c in cols if c not in ("marketplace", "asin", "start_date", "end_date", "currency")]
    df = pd.DataFrame(daily)
    for c in num:
        df[c] = pd.to_numeric(df[c])
    last_day = df["end_date"].max()
    grouped = df.groupby("asin")
    sum_cols = kind["sum_cols"] if kind["sum_cols"] is not None else num
    snap_cols = [c for c in num if c not in sum_cols]

    out = grouped[sum_cols].sum(min_count=1)
    if snap_cols:
        latest = df[df["end_date"] == last_day].set_index("asin")[snap_cols]
        out = out.join(latest.reindex(out.index))
    out["currency"] = grouped["currency"].agg(lambda s: s.dropna().iloc[0] if s.notna().any() else None)
    out = out.reset_index()
    out["marketplace"] = df["marketplace"].iloc[0]
    out["start_date"] = f"{month_start:%Y-%m-%d}"
    out["end_date"] = last_day
    return out[cols]


def is_final_in_db(table: str, mp: str, month_start: date) -> bool:
    conn = db.connect()
    try:
        cur = conn.cursor()
        # Same test as period_status = 'final', but also works on a table
        # ingest_to_db.py hasn't migrated yet (e.g. on a --dry-run).
        cur.execute(f"SELECT COUNT(*) FROM dbo.{table} "
                    f"WHERE marketplace = ? AND start_date = ? AND end_date = EOMONTH(start_date)",
                    mp, month_start)
        return cur.fetchone()[0] > 0
    except Exception as e:  # table not created yet
        if "Invalid object name" in str(e):
            return False
        raise
    finally:
        conn.close()


def fetch_marketplace(ctx: dict, kind_name: str, kind: dict, today: date) -> list[pd.DataFrame]:
    """Month frames to load for one marketplace + report type."""
    mp = ctx["mp"]
    latest = today - timedelta(days=LAG_DAYS)
    this_month = today.replace(day=1)
    prev_month = (this_month - timedelta(days=1)).replace(day=1)
    frames = []

    # 1. Previous month: make sure it ends up with Amazon's final figures.
    if is_final_in_db(kind["db"]["table"], mp, prev_month):
        print(f"  [{mp} {kind_name}] {prev_month:%Y-%m} already final in the database")
    else:
        print(f"  [{mp} {kind_name}] {prev_month:%Y-%m}: requesting the final monthly report")
        try:
            report = request(ctx, kind["report_type"], "MONTH", prev_month, month_end(prev_month))
            frames.append(pd.DataFrame(kind["flatten"](report, mp)))
            print(f"    got {len(frames[-1])} final rows")
        except (RuntimeError, TimeoutError, requests.HTTPError) as e:
            print(f"    not available yet ({e}); refreshing it from daily data instead")
            rows, last = fetch_days(ctx, kind, prev_month, min(month_end(prev_month), latest))
            if rows:
                frames.append(roll_up(rows, kind, prev_month))
                print(f"    {prev_month:%Y-%m} to date: {len(frames[-1])} rows through {last}")

    # 2. Current month to date.
    if latest < this_month:
        print(f"  [{mp} {kind_name}] no day of {this_month:%Y-%m} is published yet "
              f"(Amazon lags ~{LAG_DAYS} days)")
    else:
        print(f"  [{mp} {kind_name}] {this_month:%Y-%m}: fetching {this_month}..{latest}")
        rows, last = fetch_days(ctx, kind, this_month, latest)
        if rows:
            frames.append(roll_up(rows, kind, this_month))
            print(f"    {this_month:%Y-%m} to date: {len(frames[-1])} rows through {last}")
        else:
            print(f"    no day of {this_month:%Y-%m} is published yet")
    return frames


def connect_marketplace(mp: str):
    region = spapi.MARKETPLACE_TO_REGION.get(mp)
    if not region:
        print(f"[skip] unknown marketplace '{mp}'")
        return None
    client_id, client_secret, refresh_token, token_env = spapi.resolve_credentials(mp)
    if not (client_id and client_secret and refresh_token):
        print(f"[skip] {mp}: credentials not set in .env (needs {token_env} and a client id/secret)")
        return None
    try:
        token = with_network_retries(spapi.get_access_token_cached, client_id, client_secret, refresh_token)
    except requests.HTTPError as e:
        print(f"[{mp}] login FAILED: {e.response.status_code} {e.response.text}")
        return False
    except requests.RequestException as e:
        print(f"[{mp}] login FAILED (network): {e}")
        return False
    cfg = spapi.REGIONS[region]
    return {"mp": mp, "host": cfg["host"], "token": token, "marketplace_id": cfg["marketplaces"][mp]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--marketplaces", default="US,UK,DE,FR,IT,ES",
                        help="Comma-separated; ones without credentials are skipped")
    parser.add_argument("--only", choices=list(KINDS), help="Just one report type")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and save CSVs, but don't write to the database")
    args = parser.parse_args()

    today = datetime.now(timezone.utc).date()
    run_tag = f"{today:%Y%m%d}"
    kinds = [args.only] if args.only else list(KINDS)
    print(f"Month-to-date run {today} (latest day requested: {today - timedelta(days=LAG_DAYS)})")

    collected = {k: [] for k in kinds}
    errors = []
    for mp in [m.strip().upper() for m in args.marketplaces.split(",") if m.strip()]:
        ctx = connect_marketplace(mp)
        if ctx is None:
            continue
        if ctx is False:
            errors.append(f"{mp}: login failed")
            continue
        for kind_name in kinds:
            try:
                collected[kind_name] += fetch_marketplace(ctx, kind_name, KINDS[kind_name], today)
            except PartialFetch as e:
                errors.append(f"{mp} {kind_name}: {e} - this month was NOT loaded")
                print(f"  [{mp} {kind_name}] FAILED part-way, not loading it: {e}")
            except requests.HTTPError as e:
                errors.append(f"{mp} {kind_name}: {e.response.status_code} {e.response.text[:200]}")
                print(f"  [{mp} {kind_name}] FAILED: {e.response.status_code} {e.response.text[:200]}")
            except requests.RequestException as e:
                errors.append(f"{mp} {kind_name}: network error, not loaded: {e}")
                print(f"  [{mp} {kind_name}] FAILED (network), not loading it: {e}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for kind_name in kinds:
        frames = collected[kind_name]
        if not frames:
            print(f"[{kind_name}] nothing to load")
            continue
        spec = KINDS[kind_name]["db"]
        df = pd.concat(frames, ignore_index=True)[list(spec["columns"])]
        df["source_file"] = f"sp_api_month_to_date {run_tag}"
        df = db.prepare(df, spec)
        csv_path = OUT_DIR / f"{kind_name}_{run_tag}.csv"
        df.to_csv(csv_path, index=False)
        summary = df.groupby(["marketplace", "start_date", "end_date", "period_status"]).size()
        print(f"[{kind_name}] {len(df):,} rows (saved to {csv_path.relative_to(Path(__file__).parent)}):")
        print("  " + summary.to_string().replace("\n", "\n  "))
        if args.dry_run:
            continue
        print(f"[{kind_name}] loading ...")
        db.load(spec, df)

    if args.dry_run:
        print("Dry run - nothing written to the database.")
    if errors:
        print("\nERRORS:\n  " + "\n  ".join(errors))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
