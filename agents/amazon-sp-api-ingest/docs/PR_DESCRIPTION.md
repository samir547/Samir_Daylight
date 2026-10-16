# Amazon Vendor Central ingest: weekly refresh into Azure SQL

Builds on Vivek's `1ead331` (first load of Amazon sales and inventory into SQL Server). That
commit stays in the history. This PR keeps its goal and replaces its two scripts with the
design in `docs/data-ingestion-spec.md` (revision 2026-10-15, Samir sign-off pending).

Everything is under `agents/amazon-sp-api-ingest/`. Nothing in `agents/demand-planning/**`
changes, and no demand-planning table is touched.

## What it does

- Loads Amazon Vendor Central **sales**, **inventory** and **weekly forecast** data for US, UK,
  DE, FR, IT and ES into Azure SQL, for Power BI and the Amazon demand forecast.
- **Weekly refresh, Thursdays (UTC):** sales and inventory at 06:00, forecast at 07:00, on the
  Ubuntu VM via cron.
  - Current month: the published `WEEK` rows (Sunday to Saturday), re-pulled every run.
  - Catch-up: any published week inside Amazon's 6-week lookback that is not stored yet.
  - Previous month: its `MONTH` row, once past day 5, re-pulled until it is no longer the previous month.
  - Forecast: every weekly pull is kept as a version; the same generation date replaces only if the content differs.
- **History:** 36 months of monthly data from validated CSVs (`load_csv_to_db.py`). There is no weekly backfill; Amazon only serves about 6 weeks.
- **Nothing new from Amazon is not an error:** the run logs it, writes a `no_data` run row, changes nothing and exits 0.

## What changed from Vivek's version

| Vivek's `1ead331` | This PR | Why |
|---|---|---|
| `ingest_to_db.py` created and altered tables on every run | Retired. `db.py` never runs DDL; tables come from `sql/001` to `sql/005`, applied by hand | DDL belongs in reviewed `.sql` files |
| Daily month-to-date script (`sp_api_month_to_date.py`) | Retired. Weekly `WEEK` rows plus the `MONTH` row | Daily roll-ups were labelled `final` too early and Amazon's real monthly figures never replaced them |
| Deleted stored rows missing from the new data | Never deletes sales or inventory rows | History must not disappear if Amazon returns less |
| Committed 200 rows at a time | One transaction per marketplace, staged and merged, rolled back alone on failure | No half-loaded month |
| Any HTTP error meant "not published yet" | Only Amazon's "not yet available" response is `no_data`. Everything else fails | A real error must not exit 0 |
| No 429 retry | Retry after 30s, 60s, 120s on 429/5xx, honouring `Retry-After` | New code makes more requests |
| Tables `Daylight_*`, with `period_status` | `amazon_vendor_sales`, `amazon_vendor_inventory`, with `period_type` (`MONTH`/`WEEK`) | Names say Amazon vendor data; one grain per row |

## Files

- **Code:** `sp_api_common.py` (shared Amazon calls, retry), `vendor_report_job.py` (weekly refresh and `--sink`), `week_plan.py` (which weeks to request), `db.py` (loader), `load_csv_to_db.py`, the three report scripts, `probe_weekly_reports.py`.
- **SQL (run by hand):** `sql/001` to `sql/005` create the objects, `006` backs up the old tables, `007` is read-only checks, `008` is a fallback copy (not part of the normal path).
- **VM:** `run_weekly_sales_inventory.sh`, `run_weekly_forecast.sh`, `docs/RUNBOOK.md`.
- **Docs:** `docs/data-ingestion-spec.md`, `docs/probe-weekly-results.md`, `README.md`.
- **Tests:** `tests/test_week_plan.py`, `tests/test_db_values.py`.

## Tested

- 30 unit tests pass (`python -m pytest tests -q`): week planning (first run of a month, catch-up after missed runs, 5-Saturday months, straddling weeks, lag and lookback edges) and value conversion for the bulk insert.
- **On SQL Server:** the monthly CSV load, US, DE, FR, IT, ES and UK, sales and inventory, all `success`. Row counts match the CSVs and every marketplace has 36 months.
- **Against Amazon (probe, read only):** WEEK reports work for sales and inventory on US, UK and DE. The window limit is 7 weeks, the lookback 6 weeks, and "not yet available" is a `FATAL` report with a recognisable message.
- Wrapper scripts pass `bash -n`.

## Not tested yet

- `--weekly-refresh --sink db` against live Amazon and SQL together.
- The forecast version loader (new, replace, unchanged) and the forecast report for UK and the EU marketplaces. Only the US forecast has ever been pulled.
- The wrappers, cron and the VM setup on Ubuntu.
- Whether the previous week is available at **06:00 UTC** on a Thursday. The probe ran at about 14:00 UTC.
- The bulk insert (`fast_executemany`) on the VM's network. `DB_FAST_EXECUTEMANY=0` is the fallback.
- No database tests exist yet; they need a test schema.
- Idempotency: loading the same file twice should report `0 inserted, 0 updated`. It has not been re-run since the first successful load.

## Known gaps

- `amazon_asin_sku_map` has no FR or IT rows, so FR and IT ASINs are unmapped in the `_sku` views. UK has 28 unmapped ASINs (13,583 shipped units over three years), US 17, DE 11, ES 9.
- September 2026 is loaded for UK only. The other five marketplaces get it from the first weekly run.
- Roughly half the inventory rows have NULL stock fields (`sellable_*`, `aged_90plus_*`, and more for `open_po_units` and `unhealthy_*`). The loader stores what Amazon returned; NULL means unknown, not zero.
- Weekly rows accumulate from go-live. If more than 6 Thursdays are missed, the oldest weeks are lost for good.

## Before this goes live (manual steps)

**Already done:** `sql/001` to `sql/006` applied, the old `Daylight_*` tables backed up (the `DROP` stays commented out), the monthly history loaded.

**Krishna and Varun:**
1. Create the least-privilege SQL login from `docs/RUNBOOK.md` (it needs `DELETE` on `amazon_forecast_weekly` only), open the VM's IP on the Azure SQL firewall, and install ODBC Driver 18 and a Python venv on the VM.
2. Put `.env` on the VM, with that login in `DB_*`. Never use the admin login there.
3. Import the US forecast snapshot of 2026-09-27: `python load_csv_to_db.py --forecast-json "<file>" --marketplace US --load`.
4. Run both wrappers by hand (`MPS=DE TRIGGER=manual ./run_weekly_sales_inventory.sh`, then all marketplaces, then the forecast). Re-run one to confirm `0 inserted, 0 updated`.
5. On a Thursday, run `python probe_weekly_reports.py --lag-only` at 06:00 UTC. If the newest week is not available, move the job later in the day or to Friday.
6. Add the two cron lines from the runbook only after steps 4 and 5 pass.
7. Drop the old `Daylight_*` tables only after confirming Power BI and other agents do not read them.

**Open questions:** Samir's sign-off on the revised spec; who owns completing `amazon_asin_sku_map` for FR and IT; the alert channel for failed or stale jobs; and the SQL login owner.

## Review notes

- Start with `week_plan.py` and `db.py`; they hold the rules.
- Revision history in the spec explains every decision change, newest at the bottom.
- `.env`, CSV, raw JSON, `output/`, `logs/` and `archive/` are gitignored. No credentials are committed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01Py9cYpieRab8hMra5q47Ld
