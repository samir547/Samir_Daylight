# amazon-sp-api-ingest

Data-pull agent for Daylight's Amazon **Vendor Central** account via the Selling Partner API (SP-API). It downloads three Vendor Retail Analytics reports and writes them to CSV for downstream use (e.g. the demand forecast pipeline).

| Script | Report type | Output |
|---|---|---|
| `sp_api_sales_report.py` | `GET_VENDOR_SALES_REPORT` | `daylight_sales_report_<MP>.csv` |
| `sp_api_inventory_report.py` | `GET_VENDOR_INVENTORY_REPORT` | `daylight_inventory_report_<MP>.csv` |
| `sp_api_forecast_report.py` | `GET_VENDOR_FORECASTING_REPORT` | `forecast_snapshots/daylight_forecast_<MP>_<YYYYMMDD>.csv` |
| `probe_*.py` | diagnostics | console only |

Generated CSV and raw JSON files are git-ignored: they contain Amazon cost and inventory figures and are regenerated on every run. Never commit them or `.env`.

## Sales report (`sp_api_sales_report.py`)

Pulls Daylight's Amazon Vendor Central sales data via the SP-API and writes it to a CSV.

### What it does, step by step

1. **Authenticates** — exchanges a long-lived LWA refresh token (plus client ID/secret) for a short-lived access token via `https://api.amazon.com/auth/o2/token` (`get_access_token`). No AWS SigV4 signing is needed; SP-API's self-authorization model (post-2023) only requires the LWA access token in the `x-amz-access-token` header.
2. **Requests a report** — calls SP-API's Reports API to create a `GET_VENDOR_SALES_REPORT` (`create_report`), with:
   - `reportPeriod: MONTH` — one row per ASIN per calendar month
   - `distributorView: MANUFACTURING` — this account sells to Amazon as a manufacturer/1P vendor (the other option, `SOURCING`, doesn't apply here)
   - `sellingProgram: RETAIL`
3. **Polls until ready** — report generation is asynchronous; `poll_report` checks status every 10 seconds (up to 5 minutes) until it's `DONE`, then returns a document ID.
4. **Downloads and decompresses** — `download_report` fetches the report document (gzip-compressed JSON) and parses it.
5. **Flattens to rows** — `flatten_report` walks the report's `salesAggregate` (account-wide totals) and `salesByAsin` (per-product totals) and turns each into a flat dict via `_sales_row`. `_amount()` normalizes fields that come back either as a plain number or as `{"amount": ..., "currencyCode": ...}`.
6. **Writes the CSV** — one row per ASIN (plus one `asin: ALL` row for the account total), columns: `marketplace, asin, start_date, end_date, ordered_revenue, ordered_units, shipped_revenue, shipped_cogs, shipped_units, customer_returns, currency`.

It also dumps each request's raw report JSON to `raw_report_<MARKETPLACE>_<YYYYMM>.json` (one per date-range chunk - see below) alongside the CSV, useful for debugging if Amazon's schema doesn't match what's expected.

### Why the date window matters

`closed_month_window()` computes a range covering the last N **fully closed** calendar months — e.g. run in September, `--months 1` requests all of August, not September. This isn't cosmetic: Amazon's vendor MONTH-period reports fail with a `FATAL` processing status if the requested window reaches into the current, still-open month or the ~72-hour SLA gap right after a period closes. Using `datetime.now()` as the end of the range (the original bug) reliably triggered this.

## Setup

**Option A - `.env` file (recommended):** copy `.env.example` to `.env` in this
folder and fill in the real values. `.env` is loaded automatically at startup
(via `python-dotenv`) and is git-ignored - each person running this script
keeps their own local copy; never commit it or paste it into chat.

**Option B - PowerShell session variables** (only lasts for that terminal window):

```powershell
$env:SPAPI_CLIENT_ID = "amzn1.application-oa2-client...."      # shared app: US, DE, FR, IT, ES
$env:SPAPI_CLIENT_SECRET = "amzn1.oa2-cs.v1...."
$env:SPAPI_REFRESH_TOKEN_US = "Atzr|...."      # one refresh token per marketplace
$env:SPAPI_REFRESH_TOKEN_DE = "Atzr|...."      # likewise _FR, _IT, _ES
$env:SPAPI_CLIENT_ID_UK = "amzn1.application-oa2-client...."   # the UK has its own app
$env:SPAPI_CLIENT_SECRET_UK = "amzn1.oa2-cs.v1...."
$env:SPAPI_REFRESH_TOKEN_UK = "Atzr|...."
```

**Credentials per marketplace.** Every marketplace has its own refresh token, named after the marketplace code. US, DE, FR, IT and ES share one Amazon app; the UK has its own.

| Marketplace | Client ID / secret | Refresh token |
|---|---|---|
| US | `SPAPI_CLIENT_ID` / `SPAPI_CLIENT_SECRET` (shared app) | `SPAPI_REFRESH_TOKEN_US` |
| DE | shared app | `SPAPI_REFRESH_TOKEN_DE` |
| FR | shared app | `SPAPI_REFRESH_TOKEN_FR` |
| IT | shared app | `SPAPI_REFRESH_TOKEN_IT` |
| ES | shared app | `SPAPI_REFRESH_TOKEN_ES` |
| UK | `SPAPI_CLIENT_ID_UK` / `SPAPI_CLIENT_SECRET_UK` (own app) | `SPAPI_REFRESH_TOKEN_UK` |

Lookup rule (`resolve_credentials` in `sp_api_sales_report.py`): the client ID/secret is `SPAPI_CLIENT_ID_<MP>` / `SPAPI_CLIENT_SECRET_<MP>` if set, otherwise the shared `SPAPI_CLIENT_ID` / `SPAPI_CLIENT_SECRET`; the refresh token is always `SPAPI_REFRESH_TOKEN_<MP>`, with no region-level fallback. The region (NA for US, EU for the rest) only decides which SP-API endpoint is called. A marketplace missing any of these is skipped with a message naming the variable it needs.

> **Renamed variable:** earlier versions read `SPAPI_REFRESH_TOKEN_NA` (US) and `SPAPI_REFRESH_TOKEN_EU`. Those names are no longer used. Rename `SPAPI_REFRESH_TOKEN_NA` to `SPAPI_REFRESH_TOKEN_US` in your `.env`; the scripts print a note if they still find the old names.

Put these in `.env` only, never in chat or in git.

Either way, then create a virtual environment and install dependencies
(first time only — reuse the same .venv on later runs):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script with an execution-policy error,
run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first (or
activate via `.venv\Scripts\activate.bat` from cmd instead). On later runs
you only need to re-activate: `.venv\Scripts\Activate.ps1`.

## Running it

```powershell
python sp_api_sales_report.py --months 1 --marketplaces US --out daylight_sales_report.csv
```

Or, for a full historical backfill from a fixed starting month:

```powershell
python sp_api_sales_report.py --start-month 2024-01 --marketplaces US --out daylight_sales_report.csv
```

- `--months` — how many trailing closed calendar months to pull (default 1). Ignored if `--start-month` is given.
- `--start-month` — `YYYY-MM`, e.g. `2023-09`. Pulls every closed month from this one through the present. SP-API caps a single `GET_VENDOR_SALES_REPORT` request at 15 calendar months per call, so a longer range is automatically split into consecutive `<=15`-month requests and merged into one CSV (each chunk's raw JSON is saved separately, as `raw_report_<MARKETPLACE>_<YYYYMM>.json`, so you can see exactly what each request returned). Amazon also only keeps 36 months of lookback available for this report at all: confirmed via `probe_lookback.py` (2026-09) that **September 2023 is the earliest available month** for `GET_VENDOR_SALES_REPORT` - anything before that fails with processing status `FATAL` (not a clean rejection, and not an empty result), so don't set `--start-month` earlier than `2023-09` without re-probing (the boundary moves forward by a month roughly every month, since it's a rolling window from "today").
- `--marketplaces` — comma-separated, e.g. `US,UK` (each marketplace needs its own `SPAPI_REFRESH_TOKEN_<MP>`; the region — US→NA, all others→EU — only picks the API endpoint)
- `--out` — output CSV path template, one file per marketplace (see "Running other regions")

### Running other regions

Output is **one CSV per marketplace**. `--out` is a path template: `{mp}` is replaced by the marketplace code (the default is `daylight_sales_report_{mp}.csv` / `daylight_inventory_report_{mp}.csv`); if the template has no `{mp}`, `_<MP>` is inserted before the extension (`--out test.csv` -> `test_DE.csv`). Files are overwritten each run, and a marketplace that returns no rows never overwrites an existing file. If any chunk fails, a `WARNING ... MISSING` line lists which marketplace/month is absent.

```powershell
python sp_api_sales_report.py --start-month 2023-09 --marketplaces DE,FR,IT,ES
python sp_api_inventory_report.py --start-month 2023-09 --marketplaces DE,FR,IT,ES
```

Produces `daylight_sales_report_DE.csv`, `_FR.csv`, `_IT.csv`, `_ES.csv` (and the same for inventory). The US file from earlier runs is named `daylight_sales_report.csv`; the next US run writes `daylight_sales_report_US.csv` instead.

EU rows carry `currency` (GBP for UK, EUR for the rest) - don't sum across marketplaces without converting.

## Inventory report (`sp_api_inventory_report.py`)

Same account, same `.env`, same auth/chunking machinery - imported directly
from `sp_api_sales_report.py` rather than duplicated. Only the request's
`reportType` (`GET_VENDOR_INVENTORY_REPORT`), its response schema, and the
CSV columns differ. Per Ankush, no extra Vendor Central access is required
beyond what's already granted for the sales report (both are covered by
the same "Brand Analytics" role).

```powershell
python sp_api_inventory_report.py --months 1 --marketplaces US
python sp_api_inventory_report.py --start-month 2023-09 --marketplaces US
```

Same `--months` / `--start-month` / `--marketplaces` / `--out` flags as the
sales script (default `--out` is `daylight_inventory_report.csv`). Output
columns: `marketplace, asin, start_date, end_date, net_received_units,
net_received_cost, open_po_units, sellable_units, sellable_cost,
unsellable_units, unsellable_cost, sell_through_rate, aged_90plus_units,
aged_90plus_cost, unhealthy_units, unhealthy_cost, currency`. Raw JSON per
chunk is saved as `raw_inventory_<MARKETPLACE>_<YYYYMM>.json`.

**Confirmed 2026-09-25 via `probe_inventory_lookback.py`:** the inventory
report shares the exact same boundary as the sales report - September 2023
is the earliest available month (`DONE`), August 2023 and everything
before it comes back `FATAL`. Use `--start-month 2023-09` for the full
inventory backfill, same as the sales report.

## Forecast report (`sp_api_forecast_report.py`)

Amazon's own forward-looking weekly demand forecast per ASIN - mean plus **P70 / P80 / P90** (`GET_VENDOR_FORECASTING_REPORT`). Same account, `.env`, credential lookup and login/polling code as the sales script (imported from it). Covered by the same Brand Analytics role.

```powershell
python sp_api_forecast_report.py --marketplaces US
python sp_api_forecast_report.py --marketplaces DE,FR,IT,ES
```

Output goes to `forecast_snapshots/` (`--out-dir`): `daylight_forecast_<MP>_<YYYYMMDD>.csv` plus `raw_forecast_<MP>_<YYYYMMDD>.json`, where the date is the forecast's generation date. Columns: `marketplace, forecast_generation_date, asin, start_date, end_date, weeks_ahead, mean_units, p70_units, p80_units, p90_units`.

How it differs from the sales and inventory reports:
- **Only the latest forecast is available** - Amazon does not serve past forecasts. To build history, run it on a schedule (e.g. weekly); each run adds a dated snapshot, and re-running within the same forecast week overwrites only that week's snapshot.
- **Request options:** only `sellingProgram` (`RETAIL` default, or `FRESH` via `--selling-program`). No `reportPeriod`, `distributorView`, date window or chunking.
- **One marketplace per request** (Amazon rejects multi-marketplace requests), handled by the per-marketplace loop.
- **Availability:** refreshed forecasts appear within ~72 hours after the week ends. Times follow Pacific time.
- **Reading P-levels:** P80 = Amazon estimates an 80% probability that demand will not exceed that many units that week. The report has no P-level request option - Amazon always returns mean, P70, P80 and P90 together. Use `--levels` (e.g. `--levels p80`, or `--levels p70,p90`) to keep only some of those columns in the CSV; the raw JSON always holds all of them.
- **Unverified parser:** field names follow Amazon's documentation but have not yet been checked against a live response. Compare the first raw JSON with the CSV. The forecast horizon (number of weeks) is not stated on the report page, so confirm it from the first pull.

## Diagnostic probes

- `probe_lookback.py` - finds the earliest month `GET_VENDOR_SALES_REPORT` will actually return data for (US credentials). Confirmed 2023-09 as of 2026-09; re-run periodically since the window rolls forward.
- `probe_inventory_lookback.py` - same, for `GET_VENDOR_INVENTORY_REPORT`. Also confirmed 2023-09 (2026-09-25) - same boundary as the sales report.
- `probe_token_matrix.py` - checks which LWA client pair (shared vs UK) accepts which refresh token. Prints only the outcome per combination, never secrets.

The lookback probes just print each probed month's outcome (`DONE` vs `FATAL` vs a clean
rejection) - they don't write or overwrite any CSV or raw JSON output.

## Known limitations

- **EU marketplaces (UK, DE, FR, IT, ES) are wired but unverified.** Credentials were supplied by Ankush (2026-09-30); no EU report has been run yet. Whether `distributorView: MANUFACTURING` and the 36-month lookback boundary hold for each EU marketplace has not been confirmed - re-run the probe scripts per marketplace before a full backfill. Marketplaces with no credentials configured are skipped with a clear message rather than failing silently.
- **`distributorView: MANUFACTURING` is hardcoded.** If Daylight's Vendor Central relationship with Amazon ever changes to a sourcing/distributor model instead, this would need to switch to `SOURCING`.
- **Credential hygiene:** rotate the client secret and refresh token in Vendor Central if they were ever shared outside a secrets manager — don't hardcode them into this script or commit them anywhere.
