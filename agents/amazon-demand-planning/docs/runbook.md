# Runbook: monthly Amazon order forecast (US, UK, DE, ES, FR, IT)

For whoever looks after the VM. No modelling knowledge needed.

## What it does
Once a month, for each marketplace separately (US, UK, DE, ES, FR, IT; the EU countries are not pooled), it
reads Amazon's sales, stock and weekly forecast from the database, forecasts the units Amazon will order over
the next 6 months for each ASIN with the `replenish` method, and writes the result to
`dbo.amazon_order_forecast`. Power BI reads the view `dbo.vw_amazon_order_forecast_sku` (filter
`is_latest = 1` **and** `method = 'replenish'`: when client adjustments exist, a `replenish_unadjusted` run is
loaded after the main one and would otherwise take the latest flag).
It never changes sales, inventory, invoices or the SKU map: it only reads them.

## One-time setup on the VM
1. Clone the repository and go to `agents/amazon-demand-planning`.
2. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
3. `cp .env.example .env` and fill in `DB_SERVER`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` (the shared
   `daylight_agents` login). Leave the Azure line blank: Azure is not used by the monthly job.
4. Create the tables and view: `.venv/bin/python -m order_forecast init-db` (safe to repeat).
   Then an admin applies, by hand and in this order, `sql/005_run_explanations.sql` and
   `sql/006_view_explanations.sql` (the explanation columns, the ASIN status and run summary tables, and the
   view with the new columns). Until they are applied the forecast still loads and the log says
   `WARNING: not stored for this run: ...`. Once 006 is applied, `init-db` skips `sql/002` so it cannot
   remove the new view columns.
5. Test everything except the database write:
   `NO_LOAD=1 POOL=DE SETTINGS=config/forecast_settings.replenish.json ./scripts/run_monthly.sh`
   then read `logs/monthly_DE_<yyyymm>.log`.
6. First real run: `./scripts/run_monthly.sh`. Check the log ends with `=== done`, then check Power BI or run
   `SELECT TOP 20 * FROM dbo.vw_amazon_order_forecast_sku WHERE is_latest = 1`.

## Scheduling
The crontab lines are in `scripts/crontab.example`, one per market, on days 6 to 12 of each month:
US 10:00, UK 11:00, DE 12:00, ES 13:00, FR 14:00, IT 15:00 UTC. They are an hour apart because the script
holds one lock for all markets. Every line passes `SETTINGS=config/forecast_settings.replenish.json`; without
it a market would run the older seasonal method.
Add them to the existing crontab, never replace it (the ingest jobs are in there): `(crontab -l; cat the-new-lines) | crontab -`.
Once a month has loaded for a market the script does nothing more for it; if Amazon's last closed month is not
in the database yet, it exits and tries again the next day.

How to tell it worked: on day 13 each market has its own marker:
```
for p in US UK DE ES FR IT; do ls -l logs/.done_${p}_$(date -u +%Y%m) 2>/dev/null || echo "MISSING: $p"; done
grep -l "already running (lock held)" logs/monthly_*_$(date -u +%Y%m).log    # should print nothing
```
and `SELECT marketplace, MAX(created_at) FROM dbo.amazon_order_forecast_run GROUP BY marketplace` should show
this month for every market. If a marker is missing, read `logs/monthly_<POOL>_<yyyymm>.log` (exit code 3 means
the monthly data was not loaded in time: check the ingest) and run it by hand:
`POOL=DE SETTINGS=config/forecast_settings.replenish.json ./scripts/run_monthly.sh`.

## Reading the log: exit codes
| Code | Meaning | What to do |
|---|---|---|
| 0 | Loaded, or already loaded this month | Nothing |
| 1 | Failed twice (database or network), or another market held the lock | Read the log, fix, re-run by hand |
| 2 | Could not run: a configuration error (unsupported `POOL`, `SETTINGS` file missing, unknown method, bad adjustments file), password, or no `.venv`. Never retried | Read the `CANNOT RUN:` line, fix, re-run |
| 3 | Last closed month is not in the database yet | Nothing: it tries again tomorrow. If it persists, check the amazon-sp-api-ingest job |
| 4 | Sanity check refused to load (forecast far from the recent pace of the same ASINs) | Read the `Explain:` lines first. Only then override by hand with `.venv/bin/python -m order_forecast run-cycle --pool DE --settings config/forecast_settings.replenish.json --force` |

To re-run a month that already loaded: `FORCE_RERUN=1 ./scripts/run_monthly.sh`. A re-run replaces that month's
forecast; it never leaves two.

## Reading a run
Each run's log has three `Explain:` lines: how many ASINs were in sales, forecast (standard and young-product
backups), skipped and why, without Amazon's forecast, sales-only (stock unknown), on default habits, and not
forecast because they appear only in Amazon's forecast or only in inventory; the seasonality mode; both sanity
ratios; and which market-level defaults were used. The run folder `results/live/<POOL>/<stamp>/` holds
`forecast.csv` (with `reason_code`, `reason_detail`, `confidence` per row), `asin_decisions.csv` (every ASIN and
why it was or was not forecast) and `run_summary.json`. The codes are explained in `docs/replenish.md`.

## Changing the method
The schedule uses `config/forecast_settings.replenish.json`. Settings can be set per market under `"pools"`
(e.g. `"pools": {"ES": {"young_min_months": 2}}`). The older methods are in `config/forecast_settings.json`:
`seasonal_half`, `seasonal`, `mean12`, `mean6`, `mean3`, `prior_year`, `ses`, `naive_last`. No code change needed.

## What the numbers mean
Totals per month are the dependable output: typically within about 15 to 25 percent of actuals in testing.
Single ASINs in a single month are indicative only (Amazon orders in lumps).

## The `replenish` method (scheduled for every market)
It replays Amazon's stock, open orders and its own weekly forecast to predict when and how much Amazon will
order; `run-cycle` pulls Amazon's forecast into the snapshot itself. Needs `sql/004` (`init-db` applies it);
`sql/005`/`006` add the explanations. Client notes go in `config/order_adjustments.csv`. Details:
`docs/replenish.md`.

## The EU markets
DE, ES, FR and IT are each forecast on their own. FR and IT share stock with the other EU countries
(`docs/phase1-data-check.md`, finding 7), so their per-country orders can be biased (FR low, IT high); whether
to pool the EU is an open decision with the client. ES has under a year of history, so expect many
young-product backups (LOW confidence) and skipped ASINs there: `asin_decisions.csv` lists them.
