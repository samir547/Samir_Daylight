"""
Pull the Amazon SP-API Vendor Forecasting report (GET_VENDOR_FORECASTING_REPORT)
for Daylight's Vendor Central account: Amazon's forward-looking WEEKLY customer
demand forecast per ASIN - mean plus P70 / P80 / P90 - and write one dated
snapshot CSV per marketplace.

Same account, same .env, same credential lookup, login, polling and download
code as sp_api_sales_report.py (imported, not duplicated). What differs:

- Only the MOST RECENT forecast is available from Amazon; past forecasts cannot
  be requested. To build a history, run this on a schedule (e.g. weekly). Each
  run writes a snapshot named after the forecast's generation date, so runs
  accumulate and re-running within the same forecast week overwrites only that
  week's snapshot.
- The request takes only `sellingProgram` (RETAIL or FRESH) - no reportPeriod,
  distributorView or date window (so none of the closed-month / 15-month
  chunking applies).
- One marketplace per request (Amazon rejects multi-marketplace requests), which
  matches this script's one-request-per-marketplace loop.
- P-levels: a P80 value means Amazon estimates an 80% probability that customer
  demand will not exceed that many units in the week.
- Refreshed forecasts are available within ~72 hours after the week ends. Times
  follow Pacific time, as with the other Vendor Retail Analytics reports.

RUN (venv activated, in this folder):
    python sp_api_forecast_report.py --marketplaces US
    python sp_api_forecast_report.py --marketplaces DE,FR,IT,ES

OUTPUT (in --out-dir, default forecast_snapshots/):
    daylight_forecast_<MP>_<YYYYMMDD>.csv   (YYYYMMDD = forecast generation date)
    raw_forecast_<MP>_<YYYYMMDD>.json       (raw report, useful if the schema differs)

NOTE: the parser follows Amazon's documented field names (forecastByAsin ->
forecastGenerationDate, asin, startDate, endDate, meanForecastUnits,
p70ForecastUnits, p80ForecastUnits, p90ForecastUnits). It has not yet been run
against a live response - check the first raw JSON against the CSV.
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime, timezone

import requests

import sp_api_sales_report as spapi

REPORT_TYPE = "GET_VENDOR_FORECASTING_REPORT"

FIELDNAMES = ["marketplace", "forecast_generation_date", "asin", "start_date", "end_date",
              "weeks_ahead", "mean_units", "p70_units", "p80_units", "p90_units"]


def create_forecast_report(host: str, access_token: str, marketplace_id: str, selling_program: str) -> str:
    """Request the forecasting report. No date window is sent - Amazon returns
    the most recent weekly forecast. Retries on 429 / transient 5xx with backoff."""
    body = {
        "reportType": REPORT_TYPE,
        "marketplaceIds": [marketplace_id],
        "reportOptions": {"sellingProgram": selling_program},
    }
    delay = 30
    for attempt in range(4):
        resp = requests.post(
            f"{host}/reports/2021-06-30/reports",
            headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            print(f"  createReport returned HTTP {resp.status_code}; retrying in {delay}s...")
            time.sleep(delay)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.json()["reportId"]


def _to_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def flatten_forecast_report(report_json: dict, marketplace: str) -> list:
    """Parses the GET_VENDOR_FORECASTING_REPORT shape: a forecastByAsin list.
    forecastGenerationDate is read per entry, falling back to a top-level value."""
    top_generation = report_json.get("forecastGenerationDate")
    rows = []
    for entry in report_json.get("forecastByAsin", []) or []:
        generation = entry.get("forecastGenerationDate") or top_generation
        start = entry.get("startDate")
        g, s = _to_date(generation), _to_date(start)
        weeks_ahead = (s - g).days // 7 if g and s else None
        mean, _ = spapi._amount(entry.get("meanForecastUnits"))
        p70, _ = spapi._amount(entry.get("p70ForecastUnits"))
        p80, _ = spapi._amount(entry.get("p80ForecastUnits"))
        p90, _ = spapi._amount(entry.get("p90ForecastUnits"))
        rows.append({
            "marketplace": marketplace,
            "forecast_generation_date": generation,
            "asin": entry.get("asin", "UNKNOWN"),
            "start_date": start,
            "end_date": entry.get("endDate"),
            "weeks_ahead": weeks_ahead,
            "mean_units": mean,
            "p70_units": p70,
            "p80_units": p80,
            "p90_units": p90,
        })
    return rows


def _snapshot_stamp(rows: list) -> str:
    """YYYYMMDD of the forecast generation date, or today (UTC) if unavailable."""
    for r in rows:
        d = _to_date(r.get("forecast_generation_date"))
        if d:
            return d.strftime("%Y%m%d")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def main():
    parser = argparse.ArgumentParser(description="Pull SP-API vendor forecasting (mean/P70/P80/P90) report to CSV")
    parser.add_argument("--marketplaces", type=str, default="US", help="Comma-separated: US,UK,DE,FR,IT,ES")
    parser.add_argument("--selling-program", type=str, default="RETAIL", choices=["RETAIL", "FRESH"],
                         help="Amazon selling program for the report (default RETAIL)")
    parser.add_argument("--out-dir", type=str, default="forecast_snapshots",
                         help="Folder for dated snapshot files (created if missing)")
    parser.add_argument("--levels", type=str, default="mean,p70,p80,p90",
                         help="Comma-separated forecast columns to keep in the CSV: any of mean,p70,p80,p90 "
                              "(default: all). Amazon always returns all four; this only trims the output.")
    args = parser.parse_args()

    levels = [l.strip().lower() for l in args.levels.split(",") if l.strip()]
    bad = [l for l in levels if l not in ("mean", "p70", "p80", "p90")]
    if bad or not levels:
        sys.exit(f"--levels must be a comma-separated subset of mean,p70,p80,p90 (got '{args.levels}').")
    fieldnames = FIELDNAMES[:6] + [f"{l}_units" for l in levels]

    os.makedirs(args.out_dir, exist_ok=True)

    written = 0
    failed = []
    for mp in [m.strip().upper() for m in args.marketplaces.split(",")]:
        region = spapi.MARKETPLACE_TO_REGION.get(mp)
        if not region:
            print(f"[skip] Unknown marketplace '{mp}'")
            continue

        cfg = spapi.REGIONS[region]
        client_id, client_secret, refresh_token, token_env = spapi.resolve_credentials(mp)
        if not client_id or not client_secret:
            print(f"[skip] {mp}: no client id/secret found (set SPAPI_CLIENT_ID_{mp} / "
                  f"SPAPI_CLIENT_SECRET_{mp}, or the shared SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET).")
            continue
        if not refresh_token:
            print(f"[skip] {mp} requires {token_env} to be set - not found.")
            continue

        print(f"[{mp}] getting access token ({region} endpoint)...")
        print(f"[{mp}] credentials: "
              f"{spapi.describe_credentials(mp, client_id, client_secret, refresh_token, token_env)}")
        try:
            access_token = spapi.get_access_token_cached(client_id, client_secret, refresh_token)
        except requests.HTTPError as e:
            print(f"[{mp}] FAILED to get access token: {e.response.status_code} {e.response.text}")
            failed.append(mp)
            continue

        marketplace_id = cfg["marketplaces"][mp]
        print(f"[{mp}] requesting vendor forecasting report (sellingProgram={args.selling_program})...")
        try:
            report_id = create_forecast_report(cfg["host"], access_token, marketplace_id, args.selling_program)
            doc_id = spapi.poll_report(cfg["host"], access_token, report_id)
            report_json = spapi.download_report(cfg["host"], access_token, doc_id)
        except requests.HTTPError as e:
            print(f"[{mp}] FAILED: {e.response.status_code} {e.response.text}")
            failed.append(mp)
            continue
        except (RuntimeError, TimeoutError) as e:
            print(f"[{mp}] FAILED: {e}")
            failed.append(mp)
            continue

        rows = flatten_forecast_report(report_json, mp)
        stamp = _snapshot_stamp(rows)

        raw_path = os.path.join(args.out_dir, f"raw_forecast_{mp}_{stamp}.json")
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(report_json, f, indent=2)
        print(f"[{mp}] saved raw report to {raw_path}")

        if not rows:
            print(f"[{mp}] report contained no forecastByAsin rows - no CSV written "
                  f"(check the raw JSON; the schema may differ from the documented one).")
            failed.append(mp)
            continue

        csv_path = os.path.join(args.out_dir, f"daylight_forecast_{mp}_{stamp}.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        asins = len({r["asin"] for r in rows})
        print(f"[{mp}] wrote {len(rows)} rows ({asins} ASINs) to {csv_path}")
        written += 1

    if failed:
        print(f"\nWARNING: no forecast written for: {', '.join(failed)}")
    if not written:
        sys.exit("No forecast data retrieved for any marketplace. See messages above.")


if __name__ == "__main__":
    main()
