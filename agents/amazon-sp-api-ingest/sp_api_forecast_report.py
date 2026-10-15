"""
Pull the Amazon SP-API Vendor Forecasting report (GET_VENDOR_FORECASTING_REPORT)
for Daylight's Vendor Central account: Amazon's forward-looking WEEKLY customer
demand forecast per ASIN (mean, P70, P80, P90), and store it as a CSV
snapshot and/or a database version (data-ingestion-spec.md section 8, D5, D12).

- Only the MOST RECENT forecast is available; past forecasts cannot be
  requested. A missed week is lost, so the raw JSON of every database pull is
  archived to archive/forecast/raw_forecast_<MP>_<YYYYMMDD>.json (git-ignored)
  and its path stored in amazon_forecast_run.raw_file_path.
- A version is (marketplace, selling_program, forecast_generation_date). A new
  generation date adds a version and never touches older ones. The same date
  pulled again is compared with the stored version: identical -> "no new
  version" (no_data, exit 0); different -> replaced in one transaction,
  keeping the same forecast_run_id.
- Validation warnings (never failures): 48 contiguous weeks per ASIN,
  p70 <= p80 <= p90, no negatives. An empty report is a failure.
- The request takes only sellingProgram (RETAIL or FRESH): no date window.

RUN (venv activated, in this folder):
    python sp_api_forecast_report.py --marketplaces US                      # CSV snapshot
    python sp_api_forecast_report.py --marketplaces US,UK,DE,FR,IT,ES --sink db --trigger scheduled
    python sp_api_forecast_report.py --marketplaces US --dry-run

One-time import of a saved raw JSON (no Amazon call): load_csv_to_db.py
--forecast-json PATH --marketplace US.

OUTPUT with --sink csv (in --out-dir, default forecast_snapshots/):
    daylight_forecast_<MP>_<YYYYMMDD>.csv   (YYYYMMDD = forecast generation date)
    raw_forecast_<MP>_<YYYYMMDD>.json

Exit codes: 0 every marketplace stored a version or had no new version;
1 at least one failed or was skipped for missing credentials; 2 nothing
could run.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

import requests

import sp_api_common as common

HERE = Path(__file__).parent
REPORT_TYPE = "GET_VENDOR_FORECASTING_REPORT"
ARCHIVE_DIR = HERE / "archive" / "forecast"
FIELDNAMES = common.FORECAST_CSV_FIELDS


class ForecastError(ValueError):
    """A forecast report that cannot be stored (empty, mixed or missing dates)."""


def prepare_forecast(report_json: dict, marketplace: str):
    """Report -> (generation_date, report_last_updated, rows, warnings). Rows carry
    date objects and are ready for db.load_forecast_version. Raises ForecastError."""
    flat = common.flatten_forecast_report(report_json, marketplace)
    if not flat:
        raise ForecastError("report contains no forecastByAsin rows")
    gens = {common.to_date(r["forecast_generation_date"]) for r in flat}
    if None in gens or len(gens) != 1:
        raise ForecastError(f"expected one forecastGenerationDate, got {sorted(map(str, gens))}")
    generation = gens.pop()
    spec = report_json.get("reportSpecification") or {}
    last_updated = common.to_date(spec.get("lastUpdatedDate"))
    rows, seen = [], set()
    for r in flat:
        ws, we = common.to_date(r["start_date"]), common.to_date(r["end_date"])
        if ws is None or we is None or r["weeks_ahead"] is None:
            raise ForecastError(f"ASIN {r['asin']}: missing week dates")
        key = (r["asin"], ws)
        if key in seen:
            raise ForecastError(f"duplicate week {ws} for ASIN {r['asin']}")
        seen.add(key)
        rows.append({"asin": r["asin"], "week_start": ws, "week_end": we, "weeks_ahead": r["weeks_ahead"],
                     "mean_units": r["mean_units"], "p70_units": r["p70_units"],
                     "p80_units": r["p80_units"], "p90_units": r["p90_units"]})
    return generation, last_updated, rows, validate_forecast(rows)


def validate_forecast(rows: list[dict]) -> list[str]:
    """Section 8.3 warnings (the load goes ahead regardless)."""
    import db
    warnings = []
    by_asin = {}
    for r in rows:
        by_asin.setdefault(r["asin"], set()).add(r["weeks_ahead"])
    expected = set(range(db.EXPECTED_FORECAST_WEEKS))
    bad = [a for a, w in by_asin.items() if w != expected]
    if bad:
        warnings.append(f"{len(bad)} ASIN(s) without contiguous weeks_ahead 0..{db.EXPECTED_FORECAST_WEEKS - 1}")
    weeks = len({r["week_start"] for r in rows})
    if weeks != db.EXPECTED_FORECAST_WEEKS:
        warnings.append(f"week_count {weeks}, expected {db.EXPECTED_FORECAST_WEEKS}")
    order = neg = 0
    for r in rows:
        vals = [r[k] for k in ("mean_units", "p70_units", "p80_units", "p90_units")]
        if any(v is not None and float(v) < 0 for v in vals):
            neg += 1
        p70, p80, p90 = vals[1:]
        if None not in (p70, p80, p90) and not (float(p70) <= float(p80) <= float(p90)):
            order += 1
    if order:
        warnings.append(f"{order} row(s) violate p70 <= p80 <= p90")
    if neg:
        warnings.append(f"{neg} row(s) with a negative value")
    return warnings


def archive_raw(report_json: dict, marketplace: str, generation: date) -> str:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = ARCHIVE_DIR / f"raw_forecast_{marketplace}_{generation:%Y%m%d}.json"
    path.write_text(json.dumps(report_json, indent=2), encoding="utf-8")
    return str(path.relative_to(HERE)).replace("\\", "/")


def store_version(conn, report_json: dict, marketplace: str, selling_program: str, trigger: str,
                  raw_path_for: callable) -> str:
    """Validate, archive and load one forecast with its own run row. Returns the run status."""
    import db
    run_id = db.start_run(conn, "forecast", marketplace, trigger)
    try:
        generation, last_updated, rows, warnings = prepare_forecast(report_json, marketplace)
    except ForecastError as e:
        db.finish_run(conn, run_id, "failed", 0, 0, 0, f"forecast not stored: {e}")
        print(f"[{marketplace}] forecast FAILED: {e}")
        return "failed"
    raw_path = raw_path_for(generation)
    try:
        outcome, fid = db.load_forecast_version(conn, marketplace, selling_program, generation, last_updated,
                                                rows, run_id, raw_path)
        conn.commit()
    except Exception as e:
        conn.rollback()
        db.finish_run(conn, run_id, "failed", len(rows), 0, 0, f"load failed, rolled back: {type(e).__name__}: {e}")
        print(f"[{marketplace}] forecast load FAILED and rolled back: {type(e).__name__}: {e}")
        return "failed"
    notes = {"new": f"new version {generation}", "replaced": f"version {generation} replaced (same id)",
             "unchanged": f"no new version: {generation} already stored, identical"}[outcome]
    status = "no_data" if outcome == "unchanged" else "success"
    inserted = 0 if outcome == "unchanged" else len(rows)
    db.finish_run(conn, run_id, status, len(rows), inserted, 0,
                  "; ".join([notes, f"forecast_run_id {fid}", f"raw {raw_path}"] + warnings))
    print(f"[{marketplace}] forecast: {notes} (forecast_run_id {fid}, {len(rows)} rows)")
    for w in warnings:
        print(f"    warning: {w}")
    return status


def write_snapshot(rows: list, report_json: dict, mp: str, out_dir: str, fieldnames: list) -> None:
    stamp = next((common.to_date(r["forecast_generation_date"]).strftime("%Y%m%d") for r in rows
                  if common.to_date(r["forecast_generation_date"])), common.utc_today().strftime("%Y%m%d"))
    os.makedirs(out_dir, exist_ok=True)
    raw_path = os.path.join(out_dir, f"raw_forecast_{mp}_{stamp}.json")
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(report_json, f, indent=2)
    csv_path = os.path.join(out_dir, f"daylight_forecast_{mp}_{stamp}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"[{mp}] wrote {len(rows)} rows ({len({r['asin'] for r in rows})} ASINs) to {csv_path}; raw {raw_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Pull the SP-API vendor forecasting report",
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--marketplaces", type=str, default="US", help="Comma-separated: US,UK,DE,FR,IT,ES")
    parser.add_argument("--selling-program", type=str, default="RETAIL", choices=["RETAIL", "FRESH"])
    parser.add_argument("--out-dir", type=str, default="forecast_snapshots", help="CSV sink folder")
    parser.add_argument("--levels", type=str, default="mean,p70,p80,p90",
                        help="CSV sink only: forecast columns to keep (the database always stores all four)")
    parser.add_argument("--sink", choices=("csv", "db", "both"), default="csv")
    parser.add_argument("--trigger", choices=("scheduled", "manual"), default="manual")
    parser.add_argument("--dry-run", action="store_true", help="Show what would run; no Amazon, no database")
    args = parser.parse_args(argv)

    levels = [l.strip().lower() for l in args.levels.split(",") if l.strip()]
    if not levels or any(l not in ("mean", "p70", "p80", "p90") for l in levels):
        print(f"--levels must be a comma-separated subset of mean,p70,p80,p90 (got '{args.levels}').")
        return common.EXIT_NOTHING_RAN
    fieldnames = FIELDNAMES[:6] + [f"{l}_units" for l in levels]
    mps = [m.strip().upper() for m in args.marketplaces.split(",") if m.strip()]
    unknown = [m for m in mps if m not in common.MARKETPLACE_TO_REGION]
    if unknown or not mps:
        print(f"Unknown marketplace(s): {', '.join(unknown) or '(none given)'}")
        return common.EXIT_NOTHING_RAN

    if args.dry_run:
        print(f"DRY RUN (forecast, sink {args.sink}): no Amazon calls, no database.")
        for mp in mps:
            missing = common.credentials_status(mp)
            print(f"[{mp}] credentials: {'present' if not missing else 'MISSING - ' + missing}; would request "
                  f"{REPORT_TYPE} sellingProgram={args.selling_program}")
        return common.EXIT_OK

    conn = None
    if args.sink in ("db", "both"):
        import db
        try:
            conn = db.connect()
            db.require_objects(conn)
            for alert in db.freshness_alerts(conn, common.utc_today()):
                print(f"ALERT {alert}")
        except Exception as e:
            print(f"Database not usable, nothing run: {type(e).__name__}: {e}")
            return common.EXIT_NOTHING_RAN

    results = {}
    for mp in mps:
        missing = common.credentials_status(mp)
        if missing:
            print(f"[skip] {mp}: {missing}")
            results[mp] = "skipped"
            continue
        try:
            ctx = common.open_marketplace(mp)
            print(f"[{mp}] requesting vendor forecasting report (sellingProgram={args.selling_program})...")
            report_json = common.fetch_report(ctx, REPORT_TYPE, {"sellingProgram": args.selling_program})
        except (common.ReportFatal, TimeoutError, requests.RequestException, ValueError) as e:
            detail = (f"HTTP {e.response.status_code} {e.response.text[:200]}"
                      if isinstance(e, requests.HTTPError) and e.response is not None else f"{type(e).__name__}: {e}")
            print(f"[{mp}] FAILED: {detail}")
            if conn is not None:
                import db
                run_id = db.start_run(conn, "forecast", mp, args.trigger)
                db.finish_run(conn, run_id, "failed", message=f"request failed: {detail}"[:2000])
            results[mp] = "failed"
            continue

        status = "success"
        if args.sink in ("csv", "both"):
            rows = common.flatten_forecast_report(report_json, mp)
            if rows:
                write_snapshot(rows, report_json, mp, args.out_dir, fieldnames)
            else:
                print(f"[{mp}] report contained no forecastByAsin rows - no CSV written (check the raw JSON).")
                status = "failed"
        if conn is not None:
            db_status = store_version(conn, report_json, mp, args.selling_program, args.trigger,
                                      lambda gen, _r=report_json, _mp=mp: archive_raw(_r, _mp, gen))
            status = db_status if status != "failed" else "failed"
        results[mp] = status

    if conn is not None:
        conn.close()
    print("\nSummary: " + ", ".join(f"{mp} {s}" for mp, s in results.items()))
    if all(s == "skipped" for s in results.values()):
        return common.EXIT_NOTHING_RAN
    if any(s in ("failed", "skipped") for s in results.values()):
        return common.EXIT_FAILED
    return common.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
