"""
Shared SP-API plumbing for the Vendor Central report scripts
(data-ingestion-spec.md section 9.3, item 1).

  - credentials per marketplace, LWA token exchange (no SigV4 needed)
  - report create / poll / download, each retried on 429 and 5xx
    (30s, 60s, 120s; a longer Retry-After is honoured) and on network errors
  - FATAL reports classified by Amazon's errorDetails text: "not yet
    available" is NotAvailable (no_data, D10); anything else is a failure
  - closed-month logic with the MONTH_SAFETY_DAY margin (section 9.2), and
    15-month chunking for MONTH backfills
  - parsers for the sales, inventory and forecast report shapes
  - the per-marketplace CSV writer
  - exit codes shared by every script (section 9.3, item 8)

The report scripts and probes import from here. Nothing in this module
touches the database.
"""
from __future__ import annotations

import csv
import gzip
import json
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from week_plan import MONTH_SAFETY_DAY

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")  # SPAPI_* and DB_* from this folder's .env, if present

LWA_TOKEN_URL = "https://api.amazon.com/auth/o2/token"

REGIONS = {
    "NA": {
        "host": "https://sellingpartnerapi-na.amazon.com",
        "marketplaces": {"US": "ATVPDKIKX0DER"},
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
MARKETPLACE_TO_REGION = {mp: region for region, cfg in REGIONS.items() for mp in cfg["marketplaces"]}
ALL_MARKETPLACES = ("US", "UK", "DE", "FR", "IT", "ES")
EXPECTED_CURRENCY = {"US": "USD", "UK": "GBP", "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR"}

# Exit codes (section 9.3, item 8). EXIT_FAILED: at least one marketplace or
# chunk failed (successful marketplaces are still committed), including a
# marketplace skipped for missing credentials. EXIT_NOTHING_RAN: nothing
# could run at all (no marketplace had credentials, database unreachable or
# tables missing). The VM wrappers do not retry EXIT_NOTHING_RAN.
EXIT_OK, EXIT_FAILED, EXIT_NOTHING_RAN = 0, 1, 2

# Retry for create, poll and download (section 9.3, item 2).
RETRY_STATUS = (429, 500, 502, 503, 504)
BACKOFF_S = (30, 60, 120)
POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 600
_sleep = time.sleep  # tests replace this

MONTH_REPORT_OPTIONS = {"reportPeriod": "MONTH", "distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
WEEK_REPORT_OPTIONS = {"reportPeriod": "WEEK", "distributorView": "MANUFACTURING", "sellingProgram": "RETAIL"}
DEFAULT_REPORT_OPTIONS = MONTH_REPORT_OPTIONS


# ---------------------------------------------------------------- errors

class CredentialsMissing(Exception):
    """A marketplace has no client id/secret or refresh token in the environment."""


class ReportFatal(RuntimeError):
    """A report ended FATAL or CANCELLED. `detail` is Amazon's errorDetails text, if any."""

    def __init__(self, report_id: str, status: str, detail: str = ""):
        self.report_id, self.status, self.detail = report_id, status, detail or ""
        super().__init__(f"Report {report_id} failed with status {status}"
                         + (f": {self.detail[:300]}" if self.detail else ""))


class NotAvailable(ReportFatal):
    """FATAL with "The report data for the requested date range is not yet
    available" (probe T0). The only FATAL that counts as no_data (D10)."""


NOT_AVAILABLE_TEXT = "not yet available"


def classify_fatal(report_id: str, status: str, detail: str) -> ReportFatal:
    if status == "FATAL" and NOT_AVAILABLE_TEXT in (detail or "").lower():
        return NotAvailable(report_id, status, detail)
    return ReportFatal(report_id, status, detail)


# ---------------------------------------------------------------- retry

def with_retry(label: str, fn, *args, **kwargs):
    """Call fn; on HTTP 429/500/502/503/504 or a network error wait 30s, 60s,
    120s (or a longer numeric Retry-After) and try again, then give up and
    re-raise. Any other HTTP error is raised at once."""
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            return fn(*args, **kwargs)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in RETRY_STATUS or attempt == len(BACKOFF_S):
                raise
            wait = BACKOFF_S[attempt]
            retry_after = e.response.headers.get("Retry-After", "") if e.response is not None else ""
            if retry_after.strip().isdigit():
                wait = max(wait, int(retry_after.strip()))
            print(f"    {label}: HTTP {status}, retrying in {wait}s [{attempt + 1}/{len(BACKOFF_S)}]")
            _sleep(wait)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == len(BACKOFF_S):
                raise
            wait = BACKOFF_S[attempt]
            print(f"    {label}: network error ({type(e).__name__}), retrying in {wait}s "
                  f"[{attempt + 1}/{len(BACKOFF_S)}]")
            _sleep(wait)


# ---------------------------------------------------------------- credentials

# Refresh-token variables from before the per-marketplace naming; no longer read.
_LEGACY_TOKEN_VARS = ("SPAPI_REFRESH_TOKEN_NA", "SPAPI_REFRESH_TOKEN_EU")


def resolve_credentials(marketplace: str):
    """(client_id, client_secret, refresh_token, token_env_name) for a marketplace;
    missing parts are None. Client id/secret: SPAPI_CLIENT_ID_<MP> /
    SPAPI_CLIENT_SECRET_<MP> (UK has its own app), else the shared SPAPI_CLIENT_ID /
    SPAPI_CLIENT_SECRET. Refresh token: SPAPI_REFRESH_TOKEN_<MP> only."""
    client_id = os.environ.get(f"SPAPI_CLIENT_ID_{marketplace}") or os.environ.get("SPAPI_CLIENT_ID")
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


def describe_credentials(marketplace: str, client_id: str, client_secret: str,
                         refresh_token: str, token_env: str) -> str:
    """Secret-safe description: source variable names, the last 6 characters of the
    client id, and only the LENGTH of the secret and token - never their values."""
    def first_set(names):
        return next((n for n in names if os.environ.get(n)), "?")
    id_src = first_set([f"SPAPI_CLIENT_ID_{marketplace}", "SPAPI_CLIENT_ID"])
    sec_src = first_set([f"SPAPI_CLIENT_SECRET_{marketplace}", "SPAPI_CLIENT_SECRET"])
    return (f"client id from {id_src} (ends ...{client_id[-6:]}, len {len(client_id)}); "
            f"secret from {sec_src} (len {len(client_secret)}); "
            f"refresh token from {token_env} (len {len(refresh_token)})")


def credentials_status(marketplace: str) -> str | None:
    """None if the marketplace has a full credential set, else what is missing (no values)."""
    client_id, client_secret, refresh_token, token_env = resolve_credentials(marketplace)
    if not client_id or not client_secret:
        return (f"no client id/secret (set SPAPI_CLIENT_ID_{marketplace} / SPAPI_CLIENT_SECRET_{marketplace}, "
                f"or the shared SPAPI_CLIENT_ID / SPAPI_CLIENT_SECRET)")
    if not refresh_token:
        return f"{token_env} not set"
    return None


def _get_access_token_once(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = requests.post(LWA_TOKEN_URL, data={"grant_type": "refresh_token", "refresh_token": refresh_token,
                                              "client_id": client_id, "client_secret": client_secret},
                         timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    return with_retry("token", _get_access_token_once, client_id, client_secret, refresh_token)


_TOKEN_CACHE = {}


def get_access_token_cached(client_id: str, client_secret: str, refresh_token: str) -> str:
    """One LWA exchange per distinct (client, refresh token) pair per run."""
    key = (client_id, refresh_token)
    if key not in _TOKEN_CACHE:
        _TOKEN_CACHE[key] = get_access_token(client_id, client_secret, refresh_token)
    return _TOKEN_CACHE[key]


@dataclass
class Marketplace:
    code: str
    region: str
    host: str
    marketplace_id: str
    token: str


def open_marketplace(mp: str) -> Marketplace:
    """Log in for one marketplace. Raises CredentialsMissing, or requests.HTTPError /
    RequestException if the token exchange fails (after retries)."""
    region = MARKETPLACE_TO_REGION.get(mp)
    if not region:
        raise CredentialsMissing(f"unknown marketplace '{mp}'")
    missing = credentials_status(mp)
    if missing:
        raise CredentialsMissing(missing)
    client_id, client_secret, refresh_token, token_env = resolve_credentials(mp)
    print(f"[{mp}] getting access token ({region} endpoint)...")
    print(f"[{mp}] credentials: {describe_credentials(mp, client_id, client_secret, refresh_token, token_env)}")
    token = get_access_token_cached(client_id, client_secret, refresh_token)
    cfg = REGIONS[region]
    return Marketplace(mp, region, cfg["host"], cfg["marketplaces"][mp], token)


# ---------------------------------------------------------------- reports

def _create_report_once(host, access_token, marketplace_id, start, end, report_type, report_options):
    body = {"reportType": report_type, "marketplaceIds": [marketplace_id],
            "reportOptions": report_options or DEFAULT_REPORT_OPTIONS}
    if start:
        body["dataStartTime"] = start
    if end:
        body["dataEndTime"] = end
    resp = requests.post(f"{host}/reports/2021-06-30/reports",
                         headers={"x-amz-access-token": access_token, "Content-Type": "application/json"},
                         json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["reportId"]


def create_report(host: str, access_token: str, marketplace_id: str, start: str | None, end: str | None,
                  report_type: str = "GET_VENDOR_SALES_REPORT", report_options: dict = None) -> str:
    """createReport, retried. `start`/`end` are ISO timestamps, or None for reports
    without a date window (the forecast)."""
    return with_retry("createReport", _create_report_once, host, access_token, marketplace_id,
                      start, end, report_type, report_options)


def _get_report_once(host, access_token, report_id):
    resp = requests.get(f"{host}/reports/2021-06-30/reports/{report_id}",
                        headers={"x-amz-access-token": access_token}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_report(host: str, access_token: str, report_id: str) -> dict:
    return with_retry("getReport", _get_report_once, host, access_token, report_id)


def _download_once(host, access_token, report_document_id):
    resp = requests.get(f"{host}/reports/2021-06-30/documents/{report_document_id}",
                        headers={"x-amz-access-token": access_token}, timeout=30)
    resp.raise_for_status()
    doc = resp.json()
    file_resp = requests.get(doc["url"], timeout=60)
    file_resp.raise_for_status()
    raw = file_resp.content
    if doc.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def download_report(host: str, access_token: str, report_document_id: str) -> dict:
    return with_retry("download", _download_once, host, access_token, report_document_id)


def _error_detail(host: str, access_token: str, body: dict) -> str:
    """Amazon's errorDetails text for a FATAL report, read from its document."""
    doc_id = body.get("reportDocumentId")
    if not doc_id:
        return ""
    try:
        doc = download_report(host, access_token, doc_id)
    except (requests.RequestException, ValueError) as e:
        return f"(error document could not be read: {type(e).__name__})"
    if isinstance(doc, dict) and doc.get("errorDetails"):
        return str(doc["errorDetails"])
    return json.dumps(doc)[:1000]


def poll_report(host: str, access_token: str, report_id: str, timeout_s: int = POLL_TIMEOUT_S) -> str:
    """Wait for DONE and return the report document id. FATAL/CANCELLED raise
    NotAvailable or ReportFatal (classified by errorDetails); no answer within
    timeout_s raises TimeoutError."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        body = get_report(host, access_token, report_id)
        status = body.get("processingStatus")
        if status == "DONE":
            return body["reportDocumentId"]
        if status in ("CANCELLED", "FATAL"):
            raise classify_fatal(report_id, status, _error_detail(host, access_token, body))
        _sleep(POLL_INTERVAL_S)
    raise TimeoutError(f"Report {report_id} did not finish within {timeout_s}s")


def iso_start(d: date) -> str:
    return f"{d:%Y-%m-%d}T00:00:00Z"


def iso_end(d: date) -> str:
    return f"{d:%Y-%m-%d}T23:59:59Z"


def fetch_report(ctx: Marketplace, report_type: str, options: dict | None,
                 start: date | None = None, end: date | None = None) -> dict:
    """create + poll + download for one marketplace. Dates are whole days (UTC)."""
    report_id = create_report(ctx.host, ctx.token, ctx.marketplace_id,
                              iso_start(start) if start else None, iso_end(end) if end else None,
                              report_type=report_type, report_options=options)
    doc_id = poll_report(ctx.host, ctx.token, report_id)
    return download_report(ctx.host, ctx.token, doc_id)


# ---------------------------------------------------------------- months

def utc_today() -> date:
    return datetime.now(timezone.utc).date()


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


def last_closed_month(today: date | None = None):
    """(year, month) of the newest month whose MONTH report may be requested.
    Normally the previous calendar month; on day 1..MONTH_SAFETY_DAY (UTC) the
    month before that, because Amazon needs ~72h after month end plus the
    Pacific-time offset (section 9.2). Applies to the backfill too."""
    today = today or utc_today()
    back = 1 if today.day > MONTH_SAFETY_DAY else 2
    return _index_to_year_month(_month_index(today.year, today.month) - back)


def month_window_chunks(start_year: int, start_month: int, end_year: int, end_month: int, max_months: int = 15):
    """Split an inclusive month range into (chunk_start, chunk_end) datetimes of at
    most `max_months` calendar months each (SP-API's cap per MONTH request)."""
    start_idx, end_idx = _month_index(start_year, start_month), _month_index(end_year, end_month)
    chunks, idx = [], start_idx
    while idx <= end_idx:
        chunk_end_idx = min(idx + max_months - 1, end_idx)
        cy, cm = _index_to_year_month(idx)
        ey, em = _index_to_year_month(chunk_end_idx)
        chunks.append((_month_start(cy, cm), _month_end(ey, em)))
        idx = chunk_end_idx + 1
    return chunks


def resolve_chunks(months_back: int, start_month: str | None, today: date | None = None):
    """Calendar-aligned request chunks ending on last_closed_month(). `start_month`
    (YYYY-MM) overrides `months_back`. Raises ValueError on a bad range."""
    end_year, end_month = last_closed_month(today)
    if start_month:
        try:
            start_year, start_mon = (int(p) for p in start_month.split("-"))
        except ValueError:
            raise ValueError(f"--start-month must be in YYYY-MM format, got '{start_month}'")
    else:
        start_year, start_mon = _index_to_year_month(_month_index(end_year, end_month) - (months_back - 1))
    if _month_index(start_year, start_mon) > _month_index(end_year, end_month):
        raise ValueError(f"start month {start_year:04d}-{start_mon:02d} is after the last closed month "
                         f"({end_year:04d}-{end_month:02d}; MONTH_SAFETY_DAY = {MONTH_SAFETY_DAY}).")
    return month_window_chunks(start_year, start_mon, end_year, end_month)


# ---------------------------------------------------------------- parsers

def _amount(field):
    """Numeric fields come back as a plain number or {"amount": ..., "currencyCode": ...}."""
    if isinstance(field, dict):
        return field.get("amount"), field.get("currencyCode")
    return field, None


SALES_CSV_FIELDS = ["marketplace", "asin", "start_date", "end_date", "ordered_revenue", "ordered_units",
                    "shipped_revenue", "shipped_cogs", "shipped_units", "customer_returns", "currency"]
INVENTORY_CSV_FIELDS = ["marketplace", "asin", "start_date", "end_date", "net_received_units",
                        "net_received_cost", "open_po_units", "sellable_units", "sellable_cost",
                        "unsellable_units", "unsellable_cost", "sell_through_rate", "aged_90plus_units",
                        "aged_90plus_cost", "unhealthy_units", "unhealthy_cost", "currency"]
FORECAST_CSV_FIELDS = ["marketplace", "forecast_generation_date", "asin", "start_date", "end_date",
                       "weeks_ahead", "mean_units", "p70_units", "p80_units", "p90_units"]


def _sales_row(marketplace: str, asin: str, entry: dict) -> dict:
    ordered_revenue, currency = _amount(entry.get("orderedRevenue"))
    return {
        "marketplace": marketplace,
        "asin": asin,
        "start_date": entry.get("startDate"),
        "end_date": entry.get("endDate"),
        "ordered_revenue": ordered_revenue,
        "ordered_units": _amount(entry.get("orderedUnits"))[0],
        "shipped_revenue": _amount(entry.get("shippedRevenue"))[0],
        "shipped_cogs": _amount(entry.get("shippedCogs"))[0],
        "shipped_units": _amount(entry.get("shippedUnits"))[0],
        "customer_returns": _amount(entry.get("customerReturns"))[0],
        "currency": currency,
    }


def flatten_sales_report(report_json: dict, marketplace: str) -> list:
    """GET_VENDOR_SALES_REPORT: salesAggregate (dict or list; written as asin 'ALL')
    plus salesByAsin."""
    rows = []
    aggregate = report_json.get("salesAggregate")
    if isinstance(aggregate, dict):
        aggregate = [aggregate]
    for entry in aggregate or []:
        rows.append(_sales_row(marketplace, "ALL", entry))
    for entry in report_json.get("salesByAsin", []) or []:
        rows.append(_sales_row(marketplace, entry.get("asin", "UNKNOWN"), entry))
    return rows


def _inventory_row(marketplace: str, asin: str, entry: dict) -> dict:
    net_received_cost, currency = _amount(entry.get("netReceivedInventoryCost"))
    return {
        "marketplace": marketplace,
        "asin": asin,
        "start_date": entry.get("startDate"),
        "end_date": entry.get("endDate"),
        "net_received_units": _amount(entry.get("netReceivedInventoryUnits"))[0],
        "net_received_cost": net_received_cost,
        "open_po_units": _amount(entry.get("openPurchaseOrderUnits"))[0],
        "sellable_units": _amount(entry.get("sellableOnHandInventoryUnits"))[0],
        "sellable_cost": _amount(entry.get("sellableOnHandInventoryCost"))[0],
        "unsellable_units": _amount(entry.get("unsellableOnHandInventoryUnits"))[0],
        "unsellable_cost": _amount(entry.get("unsellableOnHandInventoryCost"))[0],
        "sell_through_rate": _amount(entry.get("sellThroughRate"))[0],
        "aged_90plus_units": _amount(entry.get("aged90PlusDaysSellableInventoryUnits"))[0],
        "aged_90plus_cost": _amount(entry.get("aged90PlusDaysSellableInventoryCost"))[0],
        "unhealthy_units": _amount(entry.get("unhealthyInventoryUnits"))[0],
        "unhealthy_cost": _amount(entry.get("unhealthyInventoryCost"))[0],
        "currency": currency,
    }


def flatten_inventory_report(report_json: dict, marketplace: str) -> list:
    """GET_VENDOR_INVENTORY_REPORT: inventoryAggregate (dict or list; written as asin
    'ALL') plus inventoryByAsin."""
    rows = []
    aggregate = report_json.get("inventoryAggregate")
    if isinstance(aggregate, dict):
        aggregate = [aggregate]
    for entry in aggregate or []:
        rows.append(_inventory_row(marketplace, "ALL", entry))
    for entry in report_json.get("inventoryByAsin", []) or []:
        rows.append(_inventory_row(marketplace, entry.get("asin", "UNKNOWN"), entry))
    return rows


def to_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def flatten_forecast_report(report_json: dict, marketplace: str) -> list:
    """GET_VENDOR_FORECASTING_REPORT: forecastByAsin. forecastGenerationDate is read
    per entry, falling back to a top-level value. weeks_ahead = whole weeks from the
    generation date to the week's start."""
    top_generation = report_json.get("forecastGenerationDate")
    rows = []
    for entry in report_json.get("forecastByAsin", []) or []:
        generation = entry.get("forecastGenerationDate") or top_generation
        start = entry.get("startDate")
        g, s = to_date(generation), to_date(start)
        rows.append({
            "marketplace": marketplace,
            "forecast_generation_date": generation,
            "asin": entry.get("asin", "UNKNOWN"),
            "start_date": start,
            "end_date": entry.get("endDate"),
            "weeks_ahead": (s - g).days // 7 if g and s else None,
            "mean_units": _amount(entry.get("meanForecastUnits"))[0],
            "p70_units": _amount(entry.get("p70ForecastUnits"))[0],
            "p80_units": _amount(entry.get("p80ForecastUnits"))[0],
            "p90_units": _amount(entry.get("p90ForecastUnits"))[0],
        })
    return rows


# ---------------------------------------------------------------- CSV

def output_path(template: str, marketplace: str) -> str:
    """Per-marketplace path: '{mp}' is replaced by the marketplace code; without
    it, '_<MP>' is inserted before the extension (report.csv -> report_DE.csv)."""
    if "{mp}" in template:
        return template.replace("{mp}", marketplace)
    root, ext = os.path.splitext(template)
    return f"{root}_{marketplace}{ext}"


def write_per_marketplace(rows_by_mp: dict, fieldnames: list, template: str, failed_chunks: list = ()) -> list:
    """Write one CSV per marketplace and return the paths written. A marketplace
    with no rows is not written, so an existing good file is never replaced by an
    empty one."""
    written = []
    for mp, rows in rows_by_mp.items():
        if not rows:
            print(f"[{mp}] no rows retrieved - no file written (any existing file is left untouched).")
            continue
        path = output_path(template, mp)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"[{mp}] wrote {len(rows)} rows to {path}")
        written.append(path)
    if failed_chunks:
        missing = ", ".join(f"{mp} {month}" for mp, month in failed_chunks)
        print(f"\nWARNING: these chunks failed and are MISSING from the output: {missing}")
    return written
