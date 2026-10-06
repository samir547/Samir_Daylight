"""
Pull the Amazon SP-API Vendor Sales report (GET_VENDOR_SALES_REPORT) for
Daylight's Vendor Central account, aggregated monthly per ASIN, in one or
more regions/marketplaces, and write the results to CSV.

SETUP (do this in your own terminal, not in a chat window):

  Option A - .env file (recommended; safe to reuse across teammates):
    Copy .env.example to .env in this folder and fill in the real values.
    .env is loaded automatically at startup (via python-dotenv) and is
    git-ignored - never commit it or paste its contents into chat.

  Option B - PowerShell session variables:
    $env:SPAPI_CLIENT_ID = "amzn1.application-oa2-client...."   # shared app: US, DE, FR, IT, ES
    $env:SPAPI_CLIENT_SECRET = "amzn1.oa2-cs.v1...."
    $env:SPAPI_REFRESH_TOKEN_US = "Atzr|...."      # one refresh token per marketplace
    $env:SPAPI_REFRESH_TOKEN_DE = "Atzr|...."      # likewise _FR, _IT, _ES
    $env:SPAPI_CLIENT_ID_UK = "amzn1.application-oa2-client...."   # UK has its own app
    $env:SPAPI_CLIENT_SECRET_UK = "amzn1.oa2-cs.v1...."
    $env:SPAPI_REFRESH_TOKEN_UK = "Atzr|...."

  Then run:
    pip install -r requirements.txt
    python sp_api_sales_report.py --months 1 --marketplaces US
    # or, for a full historical backfill from a fixed starting month:
    python sp_api_sales_report.py --start-month 2024-01 --marketplaces US

Notes:
- Every marketplace has its own refresh token (SPAPI_REFRESH_TOKEN_<MP>,
  e.g. _US, _DE, _UK). US, DE, FR, IT and ES share one app
  (SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET); UK uses its own app
  (SPAPI_CLIENT_ID_UK / SPAPI_CLIENT_SECRET_UK). A marketplace whose
  credentials aren't set is skipped with a clear message instead of
  failing silently. The region (NA vs EU) only selects the API endpoint.
- No AWS SigV4 signing is needed for this call under SP-API's
  self-authorization model (post-2023) - only the LWA access token.
- Rotate SPAPI_REFRESH_TOKEN_* / SPAPI_CLIENT_SECRET periodically,
  especially if they were ever pasted anywhere outside a secrets manager.
- SP-API caps MONTH-period reports at 15 calendar months per request and
  36 months of lookback from today. --start-month automatically splits a
  longer range into multiple <=15-month requests and merges the results,
  but data further back than 36 months won't be available at all.
"""

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()  # populates SPAPI_* env vars from a local .env file, if present

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REGIONS = {
    "NA": {
        "host": "https://sellingpartnerapi-na.amazon.com",
        "marketplaces": {
            "US": "ATVPDKIKX0DER",
        },
    },
    "EU": {
        "host": "https://sellingpartnerapi-eu.amazon.com",
        "marketplaces": {
            "UK": "A1F83G8C2ARO7P",
            "DE": "A1PA6795UKMFR9",
            "FR": "A13V1IB3VIYZZH",
            "IT": "APJ6JRA9NG5V4",
            "ES": "A1RKKUPIHCS9HS",
        },
    },
}

MARKETPLACE_TO_REGION = {
    mp: region for region, cfg in REGIONS.items() for mp in cfg["marketplaces"]
}


# Refresh-token variables from before the per-marketplace naming; no longer read.
_LEGACY_TOKEN_VARS = ("SPAPI_REFRESH_TOKEN_NA", "SPAPI_REFRESH_TOKEN_EU")


def resolve_credentials(marketplace: str):
    """Return (client_id, client_secret, refresh_token, token_env_name) for a
    marketplace, or None parts if missing. Lookup order (first found wins):

      client id/secret : SPAPI_CLIENT_ID_<MARKETPLACE> / SPAPI_CLIENT_SECRET_<MARKETPLACE>  (UK has its own app)
                         -> SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET   (shared app: US, DE, FR, IT, ES)
      refresh token    : SPAPI_REFRESH_TOKEN_<MARKETPLACE> only     (e.g. _US, _DE, _UK)

    There is no region-level fallback: every marketplace needs its own refresh
    token. The region (NA/EU) only decides which API host is called."""
    client_id = (os.environ.get(f"SPAPI_CLIENT_ID_{marketplace}")
                 or os.environ.get("SPAPI_CLIENT_ID"))
    client_secret = (os.environ.get(f"SPAPI_CLIENT_SECRET_{marketplace}")
                     or os.environ.get("SPAPI_CLIENT_SECRET"))
    token_env = f"SPAPI_REFRESH_TOKEN_{marketplace}"
    refresh_token = os.environ.get(token_env)
    if not refresh_token:
        old = [n for n in _LEGACY_TOKEN_VARS if os.environ.get(n)]
        if old:
            print(f"[{marketplace}] NOTE: {', '.join(old)} is no longer used - refresh tokens are now "
                  f"per marketplace. Rename it (or add {token_env}) in your .env.")
    return client_id, client_secret, refresh_token, token_env


_TOKEN_CACHE = {}


def describe_credentials(marketplace: str, client_id: str, client_secret: str,
                         refresh_token: str, token_env: str) -> str:
    """One-line, secret-safe description of which credentials a marketplace is
    using: the env var each came from, the last 6 chars of the client id (to tell
    apps apart), and only the LENGTH of the secret and token - never their values."""
    def first_set(names):
        return next((n for n in names if os.environ.get(n)), "?")
    id_src = first_set([f"SPAPI_CLIENT_ID_{marketplace}", "SPAPI_CLIENT_ID"])
    sec_src = first_set([f"SPAPI_CLIENT_SECRET_{marketplace}", "SPAPI_CLIENT_SECRET"])
    return (f"client id from {id_src} (ends ...{client_id[-6:]}, len {len(client_id)}); "
            f"secret from {sec_src} (len {len(client_secret)}); "
            f"refresh token from {token_env} (len {len(refresh_token)})")


def get_access_token_cached(client_id: str, client_secret: str, refresh_token: str) -> str:
    """One LWA exchange per distinct (client, refresh token) pair per run."""
    key = (client_id, refresh_token)
    if key not in _TOKEN_CACHE:
        _TOKEN_CACHE[key] = get_access_token(client_id, client_secret, refresh_token)
    return _TOKEN_CACHE[key]


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(
        LWA_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


DEFAULT_REPORT_OPTIONS = {
    "reportPeriod": "MONTH",
    "distributorView": "MANUFACTURING",
    "sellingProgram": "RETAIL",
}


def create_report(host: str, access_token: str, marketplace_id: str, start: str, end: str,
                   report_type: str = "GET_VENDOR_SALES_REPORT", report_options: dict = None) -> str:
    """Generic vendor-report request. Defaults match this script's own
    GET_VENDOR_SALES_REPORT usage; other scripts (e.g. the inventory
    report) import this and pass their own report_type/report_options."""
    resp = requests.post(
        f"{host}/reports/2021-06-30/reports",
        headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
        json={
            "reportType": report_type,
            "marketplaceIds": [marketplace_id],
            "reportOptions": report_options or DEFAULT_REPORT_OPTIONS,
            "dataStartTime": start,
            "dataEndTime": end,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["reportId"]


def poll_report(host: str, access_token: str, report_id: str, timeout_s: int = 300) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = requests.get(
            f"{host}/reports/2021-06-30/reports/{report_id}",
            headers={"x-amz-access-token": access_token},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        status = body["processingStatus"]
        if status == "DONE":
            return body["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"Report {report_id} failed with status {status}")
        time.sleep(10)
    raise TimeoutError(f"Report {report_id} did not finish within {timeout_s}s")


def download_report(host: str, access_token: str, report_document_id: str) -> dict:
    resp = requests.get(
        f"{host}/reports/2021-06-30/documents/{report_document_id}",
        headers={"x-amz-access-token": access_token},
        timeout=30,
    )
    resp.raise_for_status()
    doc = resp.json()
    file_resp = requests.get(doc["url"], timeout=60)
    file_resp.raise_for_status()
    raw = file_resp.content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def _amount(field):
    """Vendor report numeric fields come back either as a raw number or
    as {"amount": ..., "currencyCode": ...}; normalize to (value, currency)."""
    if isinstance(field, dict):
        return field.get("amount"), field.get("currencyCode")
    return field, None


def _sales_row(marketplace: str, asin: str, entry: dict) -> dict:
    ordered_revenue, currency = _amount(entry.get("orderedRevenue"))
    ordered_units, _ = _amount(entry.get("orderedUnits"))
    shipped_revenue, _ = _amount(entry.get("shippedRevenue"))
    shipped_cogs, _ = _amount(entry.get("shippedCogs"))
    shipped_units, _ = _amount(entry.get("shippedUnits"))
    customer_returns, _ = _amount(entry.get("customerReturns"))
    return {
        "marketplace": marketplace,
        "asin": asin,
        "start_date": entry.get("startDate"),
        "end_date": entry.get("endDate"),
        "ordered_revenue": ordered_revenue,
        "ordered_units": ordered_units,
        "shipped_revenue": shipped_revenue,
        "shipped_cogs": shipped_cogs,
        "shipped_units": shipped_units,
        "customer_returns": customer_returns,
        "currency": currency,
    }


def flatten_report(report_json: dict, marketplace: str) -> list:
    """Parses the GET_VENDOR_SALES_REPORT shape: a salesAggregate
    (dict or list of period totals) plus a salesByAsin list."""
    rows = []
    aggregate = report_json.get("salesAggregate")
    if isinstance(aggregate, dict):
        aggregate = [aggregate]
    for entry in aggregate or []:
        rows.append(_sales_row(marketplace, "ALL", entry))
    for entry in report_json.get("salesByAsin", []) or []:
        rows.append(_sales_row(marketplace, entry.get("asin", "UNKNOWN"), entry))
    return rows


def output_path(template: str, marketplace: str) -> str:
    """Per-marketplace output path. A '{mp}' placeholder in the template is
    replaced by the marketplace code; without one, '_<MP>' is inserted before
    the file extension (report.csv -> report_DE.csv)."""
    if "{mp}" in template:
        return template.replace("{mp}", marketplace)
    root, ext = os.path.splitext(template)
    return f"{root}_{marketplace}{ext}"


def write_per_marketplace(rows_by_mp: dict, fieldnames: list, template: str, failed_chunks: list):
    """Write one CSV per marketplace. A marketplace that returned no rows is
    not written, so an existing good file is never replaced by an empty one."""
    written = 0
    for mp, rows in rows_by_mp.items():
        if not rows:
            print(f"[{mp}] no rows retrieved - no file written (any existing file is left untouched).")
            continue
        path = output_path(template, mp)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{mp}] wrote {len(rows)} rows to {path}")
        written += 1
    if failed_chunks:
        missing = ", ".join(f"{mp} {month}" for mp, month in failed_chunks)
        print(f"\nWARNING: these chunks failed and are MISSING from the output: {missing}")
    if not written:
        sys.exit("No data retrieved for any marketplace. See messages above.")


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def _index_to_year_month(idx: int):
    return idx // 12, idx % 12 + 1


def _month_start(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=timezone.utc)


def _month_end(year: int, month: int) -> datetime:
    """Last instant (23:59:59 UTC) of the given calendar month."""
    next_year, next_month = _index_to_year_month(_month_index(year, month) + 1)
    return _month_start(next_year, next_month) - timedelta(seconds=1)


def last_closed_month():
    """(year, month) of the most recent fully-closed calendar month.
    Vendor MONTH-period reports fail FATAL if the window reaches into the
    current, still-open month or the ~72h post-close SLA gap."""
    now = datetime.now(timezone.utc)
    return _index_to_year_month(_month_index(now.year, now.month) - 1)


def month_window_chunks(start_year: int, start_month: int, end_year: int, end_month: int, max_months: int = 15):
    """Split [start_year-start_month .. end_year-end_month] (inclusive) into
    consecutive (chunk_start, chunk_end) datetime pairs of at most
    `max_months` calendar months each. SP-API caps a single MONTH-period
    request at 15 calendar months, so a multi-year pull has to be issued
    as several back-to-back requests and merged - this computes that split."""
    start_idx = _month_index(start_year, start_month)
    end_idx = _month_index(end_year, end_month)
    chunks = []
    idx = start_idx
    while idx <= end_idx:
        chunk_end_idx = min(idx + max_months - 1, end_idx)
        cy, cm = _index_to_year_month(idx)
        ey, em = _index_to_year_month(chunk_end_idx)
        chunks.append((_month_start(cy, cm), _month_end(ey, em)))
        idx = chunk_end_idx + 1
    return chunks


def resolve_chunks(months_back: int, start_month: str):
    """Resolve the requested range into calendar-aligned request chunks.
    `start_month` (YYYY-MM), if given, fixes an explicit start and takes
    priority over `months_back`; the end is always the last closed month."""
    end_year, end_month = last_closed_month()
    if start_month:
        try:
            start_year, start_mon = (int(p) for p in start_month.split("-"))
        except ValueError:
            sys.exit(f"--start-month must be in YYYY-MM format, got '{start_month}'")
    else:
        start_year, start_mon = _index_to_year_month(_month_index(end_year, end_month) - (months_back - 1))

    if _month_index(start_year, start_mon) > _month_index(end_year, end_month):
        sys.exit(f"--start-month {start_month} is after the last closed month ({end_year:04d}-{end_month:02d}).")

    return month_window_chunks(start_year, start_mon, end_year, end_month)


def main():
    parser = argparse.ArgumentParser(description="Pull SP-API vendor sales report to CSV")
    parser.add_argument("--months", type=int, default=1,
                         help="Number of trailing closed calendar months to pull (ignored if --start-month is given)")
    parser.add_argument("--start-month", type=str, default=None,
                         help="YYYY-MM, e.g. 2024-01. Pulls every closed month from this one through the present. "
                              "Overrides --months. Automatically split into <=15-month API requests and merged.")
    parser.add_argument("--marketplaces", type=str, default="US", help="Comma-separated: US,UK,DE,FR,IT,ES")
    parser.add_argument("--out", type=str, default="daylight_sales_report_{mp}.csv",
                         help="Output CSV path, one file per marketplace. '{mp}' is replaced by the "
                              "marketplace code (default -> daylight_sales_report_DE.csv, ...); without "
                              "'{mp}', '_<MP>' is inserted before the extension. Files are overwritten each run.")
    args = parser.parse_args()

    chunks = resolve_chunks(args.months, args.start_month)
    print(f"[plan] {len(chunks)} request(s) needed to cover "
          f"{chunks[0][0]:%Y-%m} through {chunks[-1][1]:%Y-%m} "
          f"(SP-API allows at most 15 calendar months per MONTH-period request)")

    rows_by_mp = {}
    failed_chunks = []
    for mp in [m.strip().upper() for m in args.marketplaces.split(",")]:
        region = MARKETPLACE_TO_REGION.get(mp)
        if not region:
            print(f"[skip] Unknown marketplace '{mp}'")
            continue

        cfg = REGIONS[region]
        client_id, client_secret, refresh_token, token_env = resolve_credentials(mp)
        if not client_id or not client_secret:
            print(f"[skip] {mp}: no client id/secret found (set SPAPI_CLIENT_ID_{mp} / "
                  f"SPAPI_CLIENT_SECRET_{mp}, or the shared SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET).")
            continue
        if not refresh_token:
            print(f"[skip] {mp} requires {token_env} to be set - not found.")
            continue

        print(f"[{mp}] getting access token ({region} endpoint)...")
        print(f"[{mp}] credentials: {describe_credentials(mp, client_id, client_secret, refresh_token, token_env)}")
        try:
            access_token = get_access_token_cached(client_id, client_secret, refresh_token)
        except requests.HTTPError as e:
            print(f"[{mp}] FAILED to get access token: {e.response.status_code} {e.response.text}")
            continue

        marketplace_id = cfg["marketplaces"][mp]
        mp_rows = rows_by_mp.setdefault(mp, [])
        for i, (chunk_start, chunk_end) in enumerate(chunks):
            start_iso = chunk_start.isoformat().replace("+00:00", "Z")
            end_iso = chunk_end.isoformat().replace("+00:00", "Z")
            print(f"[{mp}] ({i + 1}/{len(chunks)}) requesting vendor sales report for {start_iso} to {end_iso}...")
            try:
                report_id = create_report(cfg["host"], access_token, marketplace_id, start_iso, end_iso)
                doc_id = poll_report(cfg["host"], access_token, report_id)
                report_json = download_report(cfg["host"], access_token, doc_id)
            except requests.HTTPError as e:
                print(f"[{mp}] chunk {chunk_start:%Y-%m} FAILED: {e.response.status_code} {e.response.text}")
                failed_chunks.append((mp, f"{chunk_start:%Y-%m}"))
                continue
            except (RuntimeError, TimeoutError) as e:
                print(f"[{mp}] chunk {chunk_start:%Y-%m} FAILED: {e}")
                failed_chunks.append((mp, f"{chunk_start:%Y-%m}"))
                continue

            raw_path = f"raw_report_{mp}_{chunk_start:%Y%m}.json"
            with open(raw_path, "w", encoding="utf-8") as f:
                json.dump(report_json, f, indent=2)
            print(f"[{mp}] saved raw report to {raw_path}")

            rows = flatten_report(report_json, mp)
            print(f"[{mp}] got {len(rows)} rows for {chunk_start:%Y-%m} to {chunk_end:%Y-%m}")
            mp_rows.extend(rows)

            if i < len(chunks) - 1:
                time.sleep(2)  # be polite to the Reports API between back-to-back createReport calls

    fieldnames = ["marketplace", "asin", "start_date", "end_date", "ordered_revenue", "ordered_units",
                  "shipped_revenue", "shipped_cogs", "shipped_units", "customer_returns", "currency"]
    write_per_marketplace(rows_by_mp, fieldnames, args.out, failed_chunks)


if __name__ == "__main__":
    main()
