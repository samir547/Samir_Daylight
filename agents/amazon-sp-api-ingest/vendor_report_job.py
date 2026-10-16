"""
The sales and inventory report jobs (data-ingestion-spec.md sections 6, 9.1
to 9.3). sp_api_sales_report.py and sp_api_inventory_report.py are thin
entry points to main() here; the two reports differ only in report type,
parser and columns.

Two modes, per marketplace:

  Monthly (default; the backfill, section 6.2)
      --months N or --start-month YYYY-MM: whole closed months ending on the
      last closed month (MONTH_SAFETY_DAY margin), in <=15-month requests.

  --weekly-refresh (the scheduled Thursday job, section 9.1)
      WEEK windows planned by week_plan.plan_weekly_run() from the weeks already
      stored, plus the previous month's MONTH row once past MONTH_SAFETY_DAY.
      Amazon's "not yet available" FATAL is no_data; every other error is a
      failure (D10).

Sinks (--sink, default csv): csv writes one file per marketplace, as before;
db loads each marketplace in one transaction (db.py) and logs a run row in
dbo.amazon_ingest_run; both does both. --dry-run prints what would be
requested and calls neither Amazon nor the database (no credentials needed).

Exit codes (sp_api_common): 0 every marketplace succeeded or had no_data;
1 at least one failed, was partial, or was skipped for missing credentials;
2 nothing could run (no marketplace had credentials, bad arguments, or the
database is unreachable or missing its tables).
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

import requests

import sp_api_common as common
import week_plan

HERE = Path(__file__).parent

KINDS = {
    "sales": {
        "report_type": "GET_VENDOR_SALES_REPORT",
        "flatten": common.flatten_sales_report,
        "csv_fields": common.SALES_CSV_FIELDS,
        "raw_prefix": "raw_report",
        "monthly_out": "daylight_sales_report_{mp}.csv",
    },
    "inventory": {
        "report_type": "GET_VENDOR_INVENTORY_REPORT",
        "flatten": common.flatten_inventory_report,
        "csv_fields": common.INVENTORY_CSV_FIELDS,
        "raw_prefix": "raw_inventory",
        "monthly_out": "daylight_inventory_report_{mp}.csv",
    },
}

FETCH_ERRORS = (common.ReportFatal, TimeoutError, requests.RequestException, ValueError)


def weekly_csv_fields(kind: str) -> list[str]:
    """Weekly-refresh CSVs carry period_type (third column); monthly CSVs do not."""
    f = list(KINDS[kind]["csv_fields"])
    return f[:2] + ["period_type"] + f[2:]


def build_parser(kind: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"Pull the SP-API vendor {kind} report to CSV and/or the database",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--months", type=int, default=1,
                   help="Monthly mode: trailing closed months to pull (ignored with --start-month)")
    p.add_argument("--start-month", type=str, default=None,
                   help="Monthly mode: YYYY-MM; every closed month from this one on, in <=15-month requests")
    p.add_argument("--weekly-refresh", action="store_true",
                   help="Weekly mode (section 9.1): planned WEEK windows plus the previous MONTH row")
    p.add_argument("--marketplaces", type=str, default="US", help="Comma-separated: US,UK,DE,FR,IT,ES")
    p.add_argument("--out", type=str, default=None,
                   help="CSV path template with {mp} (default: daylight_<kind>_report_{mp}.csv; weekly: "
                        "output/weekly/daylight_<kind>_report_{mp}_<YYYYMMDD>.csv)")
    p.add_argument("--sink", choices=("csv", "db", "both"), default="csv", help="Where results go (default csv)")
    p.add_argument("--trigger", choices=("scheduled", "manual", "backfill"), default=None,
                   help="trigger_type for the run log (default: weekly -> manual, monthly -> backfill)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be requested; no Amazon calls, no database, no credentials needed")
    p.add_argument("--today", type=str, default=None,
                   help="With --dry-run only: plan as if today (UTC) were YYYY-MM-DD")
    return p


# ---------------------------------------------------------------- fetching

def _save_raw(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"    saved raw report to {path}")


def _fetch_rows(ctx, kind, options, start, end, period_type, raw_path):
    report = common.fetch_report(ctx, KINDS[kind]["report_type"], options, start, end)
    _save_raw(raw_path, report)
    rows = KINDS[kind]["flatten"](report, ctx.code)
    for r in rows:
        r["period_type"] = period_type
    print(f"    {period_type} {start}..{end}: {len(rows)} rows")
    return rows


class Collected:
    """What one marketplace's requests produced."""

    def __init__(self):
        self.rows, self.notes, self.errors = [], [], []
        self.ok_requests = 0
        self.window = [None, None]

    def span(self, start: date, end: date):
        self.window[0] = start if self.window[0] is None else min(self.window[0], start)
        self.window[1] = end if self.window[1] is None else max(self.window[1], end)


def collect_monthly(ctx, kind: str, chunks) -> Collected:
    c = Collected()
    for i, (cs, ce) in enumerate(chunks):
        start, end = cs.date(), ce.date()
        c.span(start, end)
        print(f"[{ctx.code}] ({i + 1}/{len(chunks)}) MONTH {start:%Y-%m}..{end:%Y-%m}")
        try:
            c.rows += _fetch_rows(ctx, kind, common.MONTH_REPORT_OPTIONS, start, end, "MONTH",
                                  HERE / f"{KINDS[kind]['raw_prefix']}_{ctx.code}_{start:%Y%m}.json")
            c.ok_requests += 1
        except FETCH_ERRORS as e:
            # In the backfill even "not yet available" is a failure: the margin should prevent it.
            c.errors.append(f"MONTH {start:%Y-%m}..{end:%Y-%m}: {_describe(e)}")
            print(f"    FAILED: {_describe(e)}")
        if i < len(chunks) - 1:
            common._sleep(2)
    return c


def collect_weekly(ctx, kind: str, plan: week_plan.WeeklyPlan, today: date) -> Collected:
    c = Collected()
    c.notes += plan.notes
    raw_dir = HERE / "output" / "weekly" / "raw"
    for start, end in plan.week_windows:
        c.span(start, end)
        try:
            c.rows += _fetch_rows(ctx, kind, common.WEEK_REPORT_OPTIONS, start, end, "WEEK",
                                  raw_dir / f"{KINDS[kind]['raw_prefix']}_{ctx.code}_week_{start:%Y%m%d}_{end:%Y%m%d}.json")
            c.ok_requests += 1
        except common.NotAvailable:
            c.notes.append(f"WEEK {start}..{end} not yet available (no_data)")
            print(f"    WEEK {start}..{end}: not yet available")
        except FETCH_ERRORS as e:
            c.errors.append(f"WEEK {start}..{end}: {_describe(e)}")
            print(f"    WEEK {start}..{end} FAILED: {_describe(e)}")
        common._sleep(2)
    if plan.month:
        start = plan.month
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        c.span(start, end)
        try:
            c.rows += _fetch_rows(ctx, kind, common.MONTH_REPORT_OPTIONS, start, end, "MONTH",
                                  raw_dir / f"{KINDS[kind]['raw_prefix']}_{ctx.code}_month_{start:%Y%m}_{today:%Y%m%d}.json")
            c.ok_requests += 1
        except common.NotAvailable:
            c.notes.append(f"MONTH {start:%Y-%m} not yet available, skipped (next run picks it up)")
            print(f"    MONTH {start:%Y-%m}: not yet available")
        except FETCH_ERRORS as e:
            c.errors.append(f"MONTH {start:%Y-%m}: {_describe(e)}")
            print(f"    MONTH {start:%Y-%m} FAILED: {_describe(e)}")
    return c


def _describe(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code} {e.response.text[:200]}"
    return f"{type(e).__name__}: {str(e)[:300]}"


# ---------------------------------------------------------------- outcome and loading

def status_without_db(c: Collected) -> str:
    if c.errors:
        return "partial" if c.ok_requests else "failed"
    return "success" if c.rows else "no_data"


def load_marketplace(conn, kind: str, mp: str, c: Collected, trigger: str, today: date) -> str:
    """Load one marketplace in one transaction and write its run row. Returns the status."""
    import db
    run_id = db.start_run(conn, kind, mp, trigger, c.window[0], c.window[1])
    inserted = updated = 0
    periods = set()
    try:
        prepared = db.prepare_vendor_rows(kind, c.rows)
        inserted, updated = db.merge_vendor_rows(conn, kind, prepared, run_id)
        conn.commit()
        periods = {(r[2], r[3]) for r in prepared}
    except Exception as e:  # roll back this marketplace only; the others continue
        conn.rollback()
        msg = "; ".join(c.errors + c.notes + [f"load failed, rolled back: {_describe(e)}"])
        db.finish_run(conn, run_id, "failed", len(c.rows), 0, 0, msg)
        print(f"[{mp}] {kind}: load FAILED and rolled back: {_describe(e)}")
        return "failed"
    if c.errors:
        status = "partial" if c.ok_requests else "failed"
    else:
        status = "success" if inserted + updated else "no_data"
    checks = []
    try:
        checks = db.quality_checks(conn, kind, mp, periods, today)
    except Exception as e:  # checks never block or undo the load
        conn.rollback()
        checks = [f"quality checks could not run: {_describe(e)}"]
    msg = "; ".join(c.errors + c.notes + checks) or None
    db.finish_run(conn, run_id, status, len(c.rows), inserted, updated, msg)
    print(f"[{mp}] {kind}: {status} - {inserted} inserted, {updated} updated (run_id {run_id})")
    for line in checks:
        print(f"    check: {line}")
    return status


def write_csv(kind: str, mp: str, rows: list, template: str, weekly: bool) -> None:
    fields = weekly_csv_fields(kind) if weekly else KINDS[kind]["csv_fields"]
    common.write_per_marketplace({mp: rows}, fields, template)


# ---------------------------------------------------------------- main

def _dry_run(kind, args, mps, today, chunks) -> int:
    print(f"DRY RUN ({kind}, {'weekly refresh' if args.weekly_refresh else 'monthly'}, sink {args.sink}, "
          f"today {today}): no Amazon calls, no database.")
    for mp in mps:
        missing = common.credentials_status(mp)
        print(f"[{mp}] credentials: {'present' if not missing else 'MISSING - ' + missing}")
        if args.weekly_refresh:
            plan = week_plan.plan_weekly_run(today, None)
            for s, e in plan.week_windows:
                print(f"    would request WEEK {s}..{e} ({((e - s).days + 1) // 7} weeks)")
            print(f"    would request MONTH {plan.month:%Y-%m}" if plan.month else "    no MONTH request")
            for n in plan.notes:
                print(f"    note: {n}")
            if args.sink != "csv":
                print("    note: with the database the plan skips weeks already stored.")
        else:
            for cs, ce in chunks:
                print(f"    would request MONTH {cs:%Y-%m}..{ce:%Y-%m}")
    return common.EXIT_OK


def main(kind: str, argv=None) -> int:
    args = build_parser(kind).parse_args(argv)
    if args.today and not args.dry_run:
        print("--today is only allowed with --dry-run.")
        return common.EXIT_NOTHING_RAN
    try:
        today = date.fromisoformat(args.today) if args.today else common.utc_today()
    except ValueError:
        print(f"--today must be YYYY-MM-DD, got '{args.today}'")
        return common.EXIT_NOTHING_RAN
    mps = [m.strip().upper() for m in args.marketplaces.split(",") if m.strip()]
    unknown = [m for m in mps if m not in common.MARKETPLACE_TO_REGION]
    if unknown or not mps:
        print(f"Unknown marketplace(s): {', '.join(unknown) or '(none given)'}; use {','.join(common.ALL_MARKETPLACES)}")
        return common.EXIT_NOTHING_RAN
    chunks = None
    if not args.weekly_refresh:
        try:
            chunks = common.resolve_chunks(args.months, args.start_month, today)
        except ValueError as e:
            print(str(e))
            return common.EXIT_NOTHING_RAN
        print(f"[plan] {len(chunks)} MONTH request(s) per marketplace covering "
              f"{chunks[0][0]:%Y-%m} through {chunks[-1][1]:%Y-%m} (<=15 months each)")
    if args.dry_run:
        return _dry_run(kind, args, mps, today, chunks)

    trigger = args.trigger or ("manual" if args.weekly_refresh else "backfill")
    template = args.out or (str(HERE / "output" / "weekly" / f"daylight_{kind}_report_{{mp}}_{today:%Y%m%d}.csv")
                            if args.weekly_refresh else KINDS[kind]["monthly_out"])
    conn = None
    if args.sink in ("db", "both"):
        import db
        try:
            conn = db.connect()
            db.require_objects(conn)
        except Exception as e:
            print(f"Database not usable, nothing run: {_describe(e)}")
            return common.EXIT_NOTHING_RAN
        if args.weekly_refresh:
            try:
                for alert in db.freshness_alerts(conn, today):
                    print(f"ALERT {alert}")
            except Exception as e:
                conn.rollback()
                print(f"ALERT freshness check could not run: {_describe(e)}")

    results = {}
    for mp in mps:
        missing = common.credentials_status(mp)
        if missing:
            print(f"[skip] {mp}: {missing}")
            results[mp] = "skipped"
            continue
        try:
            ctx = common.open_marketplace(mp)
        except requests.RequestException as e:
            print(f"[{mp}] login FAILED: {_describe(e)}")
            if conn is not None:
                import db
                run_id = db.start_run(conn, kind, mp, trigger)
                db.finish_run(conn, run_id, "failed", message=f"login failed: {_describe(e)}")
            results[mp] = "failed"
            continue

        if args.weekly_refresh:
            stored = None
            if conn is not None:
                import db
                stored = db.stored_week_ends(conn, kind, mp)
            plan = week_plan.plan_weekly_run(today, stored)
            print(f"[{mp}] {kind} weekly plan: windows {plan.week_windows or 'none'}, "
                  f"MONTH {plan.month:%Y-%m}" if plan.month else
                  f"[{mp}] {kind} weekly plan: windows {plan.week_windows or 'none'}, no MONTH")
            for n in plan.notes:
                print(f"    note: {n}")
            c = collect_weekly(ctx, kind, plan, today)
        else:
            c = collect_monthly(ctx, kind, chunks)

        if args.sink in ("csv", "both"):
            write_csv(kind, mp, c.rows, template, args.weekly_refresh)
        results[mp] = (load_marketplace(conn, kind, mp, c, trigger, today) if conn is not None
                       else status_without_db(c))
        if args.sink == "csv":
            print(f"[{mp}] {kind}: {results[mp]}")

    if conn is not None:
        conn.close()
    print("\nSummary: " + ", ".join(f"{mp} {s}" for mp, s in results.items()))
    if all(s == "skipped" for s in results.values()):
        return common.EXIT_NOTHING_RAN
    if any(s in ("failed", "partial", "skipped") for s in results.values()):
        return common.EXIT_FAILED
    return common.EXIT_OK
