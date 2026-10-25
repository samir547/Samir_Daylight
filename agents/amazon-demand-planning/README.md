# amazon-demand-planning

Predicts the orders Amazon will place on Daylight, monthly per ASIN, from Amazon's
own data (sales, inventory, Amazon's forecast). Separate from the non-Amazon
`demand-planning` agent. Out of scope: Daylight's stock, lead times, MOQs.
Power BI is the interface for the plan.

Status: **back-test harness for US and UK (baseline forecasters). Azure AutoML arm,
storage and Power BI views are next.** Background and data findings:
[docs/phase1-data-check.md](docs/phase1-data-check.md).

## Design: three layers that do not know about each other

| Layer | Keyed by | Uses the SKU map? |
|---|---|---|
| 1. Forecast (`series`, `baselines`, `backtest`, stored forecasts) | marketplace + ASIN + month | **No.** Not imported, no `product_code` column. |
| 2. Actuals (`invoices`, `accounts`, `config/amazon_accounts.csv`) | marketplace + item code + month | No |
| 3. Join views (`sql/002_views.sql`) | whatever the consumer needs | **Yes. The only place.** |

Consequences: correcting the map changes the views at once and never a stored
forecast. Switching model (baselines, Azure AutoML) touches layer 1 only.
Samir's account decisions are edits to `config/amazon_accounts.csv`.

Answer keys, none of which is ever a model input:

- `sellout`: the series being forecast (ASIN-month).
- `receipts`: Amazon's `net_received_units` (ASIN-month). Needs no map.
- `invoices`: Daylight's gross invoiced units, compared at marketplace-month level. Needs no map.

Gross units (Qty > 0) are used for invoices (Phase 1b: credits are under 1.5%
since 2024 and the large earlier ones are bulk returns, not reversed orders).

## Run it (PowerShell, from this folder)

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
python -m pytest                                    # offline, no database

# 1. read-only pull (SELECT only; use a read-only login if you have one)
python -m order_forecast pull --pool US --env-file ..\amazon-sp-api-ingest\.env

# 2. offline back-test on the snapshot it prints
python -m order_forecast backtest --snapshot snapshots\US\<stamp>
python -m order_forecast backtest --snapshot snapshots\US\<stamp> --target shipped_units
```

Defaults: horizon 6, at least 24 months of training, non-overlapping windows
(with 36 months that is two windows), series need 12 months of history to be scored.
`--stride 1` gives seven overlapping windows for a steadier but optimistic read.

## Known limits

- Only US and UK. The EU needs a (marketplace, ASIN) key and pooling; not built.
- Two windows is a thin test. Treat differences of a few WAPE points as noise.
- Marketplace-month results compare scored series with totals for all series, so bias reads low by the uncovered share (printed).
- `pending` accounts (`AMAZFB13`, Canada `30115`) are excluded unless `--include-pending`.
- Amazon's own forecast has one version only, so it cannot be back-tested yet.
- The SQL in `sql/` has not been run.
