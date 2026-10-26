# Runbook: monthly Amazon order forecast (US)

For whoever looks after the VM. No modelling knowledge needed.

## What it does
Once a month it reads Amazon's sales and stock data from the database, forecasts the units Amazon will order
over the next 6 months for each ASIN, and writes the result to `dbo.amazon_order_forecast`. Power BI reads the
view `dbo.vw_amazon_order_forecast_sku` (filter `is_latest = 1`).
It never changes sales, inventory, invoices or the SKU map: it only reads them.

## One-time setup on the VM
1. Clone the repository and go to `agents/amazon-demand-planning`.
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (the shared
   `daylight_agents` login). Leave the Azure line blank: Azure is not used by the monthly job.
4. Create the tables and view: `.venv/bin/python -m order_forecast init-db` (safe to repeat).
5. Test everything except the database write: `NO_LOAD=1 ./scripts/run_monthly.sh` then read `logs/monthly_US_<yyyymm>.log`.
6. First real run: `./scripts/run_monthly.sh`. Check the log ends with `=== done`, then check Power BI or run
   `SELECT TOP 20 * FROM dbo.vw_amazon_order_forecast_sku WHERE is_latest = 1`.

## Scheduling (not switched on yet)
`scripts/crontab.example` holds the line to add with `crontab -e`. It runs at 10:00 UTC on days 6 to 12 of each
month. Once a month has loaded the script does nothing more; if Amazon's last closed month is not in the
database yet, it exits and tries again the next day. It does not clash with the Thursday ingest jobs.

## Reading the log: exit codes
| Code | Meaning | What to do |
|---|---|---|
| 0 | Loaded, or already loaded this month | Nothing |
| 1 | Failed twice (database or network) | Read the log, fix, re-run by hand |
| 2 | Could not start (settings, password, no `.venv`) | Fix `.env` or setup, re-run |
| 3 | Last closed month is not in the database yet | Nothing: it tries again tomorrow. If it persists, check the amazon-sp-api-ingest job |
| 4 | Sanity check refused to load (forecast far from the recent pace) | Check the data first. Only then override by hand with `.venv/bin/python -m order_forecast run-cycle --pool US --force` |

To re-run a month that already loaded: `FORCE_RERUN=1 ./scripts/run_monthly.sh`. A re-run replaces that month's
forecast; it never leaves two.

## Changing the method
Edit `config/forecast_settings.json` (`method`, `horizon`, `target`). Methods: `seasonal_half` (default),
`seasonal`, `mean12`, `mean6`, `mean3`, `prior_year`, `ses`, `naive_last`. No code change needed.

## What the numbers mean
Totals per month are the dependable output: typically within about 15 to 25 percent of actuals in testing.
Single ASINs in a single month are indicative only (Amazon orders in lumps).
