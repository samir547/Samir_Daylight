"""
Load the validated backfill CSVs into the spec tables without calling Amazon
(data-ingestion-spec.md sections 6, 7.6 step 4, 9.3 item 7; task T5).

    daylight_sales_report*.csv     -> dbo.amazon_vendor_sales
    daylight_inventory_report*.csv -> dbo.amazon_vendor_inventory

Monthly CSVs (the report scripts' normal output, no period_type column) are
loaded as period_type 'MONTH', after checking every row is a whole calendar
month. CSVs written by --weekly-refresh carry a period_type column and are
loaded as given. This is the only way history gets into the spec tables:
nothing is copied from the 2026-10-11 Daylight_* tables or their backups.

By DEFAULT IT ONLY LISTS AND VALIDATES: every file found, its marketplace,
row count, grain and first..last period, and any problem. Nothing is written
unless --load is given. With --load, each (report, marketplace) is one
transaction and one amazon_ingest_run row (trigger_type 'backfill'),
upserted on the natural key (unchanged rows are not touched, nothing is
deleted), followed by the data quality checks and the month coverage check
for every marketplace.

Also imports a saved raw forecast JSON as a forecast version (section 8.6),
trigger_type 'import'; the file is copied to archive/forecast/ first.

    python load_csv_to_db.py --csv-dir "C:/path/to/csvs"            # list + validate only
    python load_csv_to_db.py --csv-dir "C:/path/to/csvs" --load     # load
    python load_csv_to_db.py --csv-dir output --only sales --marketplaces UK --load
    python load_csv_to_db.py --forecast-json "C:/path/raw_forecast_US_20260927.json" --marketplace US --load

Exit codes: 0 everything loaded (or listed without problems); 1 at least one
load failed; 2 nothing could run (no files, validation problems, database
unusable).
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

import sp_api_common as common
import db

HERE = Path(__file__).parent
PATTERNS = {"sales": "daylight_sales_report*.csv", "inventory": "daylight_inventory_report*.csv"}
CSV_FIELDS = {"sales": common.SALES_CSV_FIELDS, "inventory": common.INVENTORY_CSV_FIELDS}


def weekly_fields(kind: str) -> list[str]:
    f = list(CSV_FIELDS[kind])
    return f[:2] + ["period_type"] + f[2:]


def read_file(kind: str, path: Path) -> dict:
    """Read and validate one CSV. Returns a summary dict with the raw rows and any problem."""
    info = {"file": path, "kind": kind, "rows": [], "problem": None}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        info["rows"] = list(reader)
    if header not in (CSV_FIELDS[kind], weekly_fields(kind)):
        info["problem"] = f"unexpected columns {header}"
        return info
    rows = info["rows"]
    info["marketplaces"] = sorted({r["marketplace"] for r in rows})
    try:
        prepared = db.prepare_vendor_rows(kind, rows, "MONTH")
    except db.DataError as e:
        info["problem"] = str(e)
        return info
    info["prepared"] = prepared
    by_grain = defaultdict(set)
    for p in prepared:
        by_grain[p[2]].add(p[3])
    info["grains"] = {g: (min(s), max(s), len(s)) for g, s in by_grain.items()}
    info["all_rows"] = sum(1 for p in prepared if p[1] == "ALL")
    info["currencies"] = sorted({p[-1] for p in prepared if p[-1]})
    bad_cur = [m for m in info["marketplaces"] if m in common.EXPECTED_CURRENCY
               and any(c != common.EXPECTED_CURRENCY[m] for c in {p[-1] for p in prepared if p[0] == m and p[-1]})]
    if bad_cur:
        info["warning"] = f"unexpected currency for {', '.join(bad_cur)}"
    unknown = [m for m in info["marketplaces"] if m not in common.ALL_MARKETPLACES]
    if unknown:
        info["problem"] = f"unknown marketplace(s) {unknown}"
    return info


def describe(info: dict) -> str:
    name = info["file"].name
    if info.get("problem") and "grains" not in info:
        return f"  {name}: PROBLEM - {info['problem']}"
    grains = "; ".join(f"{g} {a:%Y-%m-%d}..{b:%Y-%m-%d} ({n} periods)" for g, (a, b, n) in sorted(info["grains"].items()))
    line = (f"  {name}: {info['kind']}, {','.join(info['marketplaces'])}, {len(info['rows'])} rows, {grains}, "
            f"{info['all_rows']} ALL rows, currency {','.join(info['currencies']) or '-'}")
    if info.get("warning"):
        line += f" - WARNING {info['warning']}"
    if info.get("problem"):
        line += f" - PROBLEM {info['problem']}"
    return line


def combine(infos: list[dict]):
    """(kind, marketplace) -> prepared rows, merged across files. A key in two
    files with different values is a conflict; identical duplicates are dropped."""
    groups, conflicts = defaultdict(dict), []
    for info in infos:
        for p in info["prepared"]:
            key = p[:4]
            bucket = groups[(info["kind"], p[0])]
            if key in bucket and bucket[key][0] != p:
                conflicts.append(f"{info['kind']} {key} differs between {bucket[key][1]} and {info['file'].name}")
            bucket.setdefault(key, (p, info["file"].name))
    return {k: [v[0] for v in d.values()] for k, d in groups.items()}, conflicts


def load_csvs(args) -> int:
    csv_dir = Path(args.csv_dir)
    if not csv_dir.is_dir():
        print(f"No such folder: {csv_dir}")
        return common.EXIT_NOTHING_RAN
    kinds = [args.only] if args.only else list(PATTERNS)
    wanted = {m.strip().upper() for m in args.marketplaces.split(",")} if args.marketplaces else None
    infos = []
    print(f"CSV files in {csv_dir.resolve()}:")
    for kind in kinds:
        for path in sorted(csv_dir.glob(PATTERNS[kind])):
            info = read_file(kind, path)
            if wanted and info.get("marketplaces") and not set(info["marketplaces"]) & wanted:
                print(f"  {path.name}: skipped (marketplace {','.join(info['marketplaces'])} not selected)")
                continue
            infos.append(info)
            print(describe(info))
    if not infos:
        print("  none found")
        return common.EXIT_NOTHING_RAN
    problems = [i for i in infos if i.get("problem")]
    groups, conflicts = combine([i for i in infos if not i.get("problem")])
    if wanted:
        groups = {k: v for k, v in groups.items() if k[1] in wanted}
    print("\nWould load (one transaction and run row each):")
    for (kind, mp), rows in sorted(groups.items()):
        months = sorted({r[3] for r in rows if r[2] == "MONTH"})
        weeks = sorted({r[3] for r in rows if r[2] == "WEEK"})
        span = (f"MONTH {months[0]:%Y-%m}..{months[-1]:%Y-%m} ({len(months)} months)" if months else "no MONTH")
        span += f", WEEK {weeks[0]}..{weeks[-1]} ({len(weeks)} weeks)" if weeks else ""
        print(f"  {kind} {mp}: {len(rows)} rows, {span}")
    for c in conflicts[:20]:
        print(f"  CONFLICT {c}")
    if problems or conflicts:
        print("\nNot loading: fix the problems/conflicts above first.")
        return common.EXIT_NOTHING_RAN
    if not args.load:
        print("\nListed and validated only - nothing written. Re-run with --load to load.")
        return common.EXIT_OK

    try:
        conn = db.connect()
        db.require_objects(conn)
    except Exception as e:
        print(f"Database not usable, nothing loaded: {type(e).__name__}: {e}")
        return common.EXIT_NOTHING_RAN
    today = common.utc_today()
    failed = False
    for (kind, mp), rows in sorted(groups.items()):
        starts = sorted(r[3] for r in rows)
        run_id = db.start_run(conn, kind, mp, "backfill", starts[0], max(r[4] for r in rows))
        try:
            inserted, updated = db.merge_vendor_rows(conn, kind, rows, run_id)
            conn.commit()
        except Exception as e:
            conn.rollback()
            db.finish_run(conn, run_id, "failed", len(rows), 0, 0, f"load failed, rolled back: {type(e).__name__}: {e}")
            print(f"[{mp}] {kind}: FAILED and rolled back: {type(e).__name__}: {e}")
            failed = True
            continue
        try:
            checks = db.quality_checks(conn, kind, mp, {(r[2], r[3]) for r in rows}, today)
        except Exception as e:
            conn.rollback()
            checks = [f"quality checks could not run: {type(e).__name__}: {e}"]
        status = "success" if inserted + updated else "no_data"
        db.finish_run(conn, run_id, status, len(rows), inserted, updated,
                      "; ".join([f"backfill from CSV ({len(rows)} rows)"] + checks))
        print(f"[{mp}] {kind}: {status} - {inserted} inserted, {updated} updated (run_id {run_id})")
        for line in checks:
            print(f"    check: {line}")

    print("\nMonth coverage after the load (at least 36 months; last month vs last closed month):")
    for kind in kinds:
        try:
            for line in db.coverage_lines(kind, db.month_coverage(conn, kind, today, common.ALL_MARKETPLACES)):
                print(f"  {line}")
        except Exception as e:
            conn.rollback()
            print(f"  {kind}: coverage check could not run: {type(e).__name__}: {e}")
    conn.close()
    return common.EXIT_FAILED if failed else common.EXIT_OK


def import_forecast(args) -> int:
    import sp_api_forecast_report as fc
    src = Path(args.forecast_json)
    mp = (args.marketplace or "").upper()
    if mp not in common.ALL_MARKETPLACES:
        print("--forecast-json needs --marketplace (US, UK, DE, FR, IT or ES).")
        return common.EXIT_NOTHING_RAN
    try:
        report = json.loads(src.read_text(encoding="utf-8"))
        generation, last_updated, rows, warnings = fc.prepare_forecast(report, mp)
    except (OSError, ValueError) as e:
        print(f"Cannot use {src}: {type(e).__name__}: {e}")
        return common.EXIT_NOTHING_RAN
    print(f"{src.name}: {mp}, generation date {generation}, last updated {last_updated}, {len(rows)} rows, "
          f"{len({r['asin'] for r in rows})} ASINs, {len({r['week_start'] for r in rows})} weeks")
    for w in warnings:
        print(f"  warning: {w}")
    if not args.load:
        print("Listed and validated only - nothing written. Re-run with --load to import.")
        return common.EXIT_OK
    try:
        conn = db.connect()
        db.require_objects(conn)
    except Exception as e:
        print(f"Database not usable, nothing loaded: {type(e).__name__}: {e}")
        return common.EXIT_NOTHING_RAN

    def archive(gen):
        fc.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        dest = fc.ARCHIVE_DIR / f"raw_forecast_{mp}_{gen:%Y%m%d}.json"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        return str(dest.relative_to(HERE)).replace("\\", "/")

    status = fc.store_version(conn, report, mp, args.selling_program, "import", archive)
    conn.close()
    return common.EXIT_OK if status in ("success", "no_data") else common.EXIT_FAILED


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Load validated backfill CSVs (or a raw forecast JSON) into the database",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--csv-dir", default=str(HERE / "output"), help="Folder with the CSVs (default output/)")
    p.add_argument("--only", choices=list(PATTERNS), help="Just one report type")
    p.add_argument("--marketplaces", default=None, help="Comma-separated filter, e.g. UK")
    p.add_argument("--load", action="store_true", help="Actually write to the database (default: list only)")
    p.add_argument("--forecast-json", default=None, help="Import this raw forecast JSON instead of CSVs")
    p.add_argument("--marketplace", default=None, help="Marketplace of --forecast-json")
    p.add_argument("--selling-program", default="RETAIL", choices=["RETAIL", "FRESH"])
    args = p.parse_args(argv)
    return import_forecast(args) if args.forecast_json else load_csvs(args)


if __name__ == "__main__":
    sys.exit(main())
