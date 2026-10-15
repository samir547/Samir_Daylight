# Amazon Vendor Central Data Ingestion — Requirements and Implementation Spec

## Revision history

| Date | Summary | Approval |
|---|---|---|
| 2026-10-15 | The weekly job now loads **weekly (WEEK) rows** for the previous and current month, plus the previous month's MONTH row once Amazon publishes it. Before this change only closed months were loaded. Both grains are kept (D8). Sums must never mix grains (D9). A run with nothing new is logged as `no_data` and is not a failure (D10). Scheduling moves to **bash and cron on Ubuntu**, Wednesdays (D11). Forecast rules restated (D12). New scope covers a weekly history backfill and moving the two tables loaded on 2026-10-11 to the spec names. Amazon facts about weekly reports are marked "to be confirmed by probe (T0)". Vivek's daily month-to-date approach (`sp_api_month_to_date.py`) is superseded. **Why:** the business needs the in-progress month without waiting for it to close, and the VM turned out to be Ubuntu. | Approved by Krishna. Samir sign-off pending. |
| 2026-10-15 (b) | Clarifications before implementation. `no_data` now also covers a load that inserts and updates nothing (D10). The previous month's MONTH row is re-requested on every run while it is still the previous month, so restatements are caught (D1). A forecast pulled again under the same generation date is compared with the stored version: identical means "no new version", different means replace (D5, section 8.4). Safety constant renamed `MONTH_SAFETY_DAY`. Database settings use the `DB_*` names already used by the demand-planning agent (O6 closed). Backfill acceptance is now "at least 36 months ending Sep 2026"; the existing Sep 2023 rows are kept. | Approved by Krishna. Samir sign-off pending. |

## 1. Purpose

Daylight sells to Amazon as a 1P vendor, so Amazon Vendor Central reports are the real sell-out and stock signal for the Amazon channel. Today three scripts pull those reports to CSV by hand. The goal of this phase is to:

1. Pull the full available history once: 36 months of monthly data, and weekly data as far back as Amazon allows.
2. Store everything in Azure SQL so other agents and Power BI can use it.
3. Keep sales and inventory current automatically, weekly, including the month in progress (through weekly rows).
4. Keep every weekly Amazon demand forecast as a separate version so forecast accuracy can be measured over time.

Downstream consumers are the `amazon-demand-forecast` agent and Power BI. The measure that matters most for demand planning is `shipped_units` (sell-out).

## 2. Current state

- Three scripts exist and work from the command line: `sp_api_sales_report.py`, `sp_api_inventory_report.py`, `sp_api_forecast_report.py`. Three probe scripts support diagnostics.
- They write CSV and raw JSON to the working folder. Nothing is scheduled.
- On 2026-10-11 Vivek's branch added `ingest_to_db.py` and `sp_api_month_to_date.py`. These loaded CSVs into `dbo.Daylight_sales_report` and `dbo.Daylight_inventory_report`, and built a month-to-date figure from daily (DAY) reports. That design is superseded by this revision (see D1 and section 10). The two tables are to be migrated to the spec names (section 7.6).
- Credentials come from `.env`, one refresh token per marketplace (see README, "Credentials per marketplace").
- Marketplaces: **US, UK, DE, FR, IT, ES**. US, DE, FR, IT and ES share one Amazon app. UK has its own app.
- Reports use `distributorView: MANUFACTURING`, `sellingProgram: RETAIL`, `reportPeriod: MONTH` (sales and inventory). This revision adds `reportPeriod: WEEK`.
- Status of marketplaces: US, DE, FR, IT, ES have produced sales and inventory CSVs. UK is proven for one month only (Aug 2026). The forecast report has been run for US only (generation date 2026-09-27, 48 weeks).
- Existing table in the target database: `dbo.amazon_asin_sku_map` (columns `asin`, `market`, `product_code`, `family`, `product_description`, `loaded_at`). It maps an Amazon ASIN to Daylight's product code per marketplace. Example: ASIN `B002ZOEVEA` maps to `E53047` in DE and ES and `D53047` in UK.

## 3. Decisions already made

| # | Topic | Decision |
|---|---|---|
| D1 | What each weekly sales and inventory run pulls | **Replaced 2026-10-15.** For each marketplace, every run pulls: (1) **WEEK rows** (`reportPeriod = WEEK`, Sunday to Saturday) for every week whose **Saturday** falls in the previous or the current calendar month. The window starts on the Sunday on or before the 1st of the previous month. It ends at the latest closed Saturday Amazon has published. The whole window is re-pulled each run, to catch restatements. The loader updates a row only if a value changed. (2) The **MONTH row for the previous calendar month**, once past the day-5 safety margin (section 9.2). It is requested on **every** run while that month is still the previous month, even if it is already stored, so restatements are picked up; the loader updates only changed rows. If it is not yet available, it is skipped and the WEEK rows cover that month; the next run picks it up. The in-progress month is covered by the weekly rows, not by daily reports. The daily month-to-date approach (`sp_api_month_to_date.py`) is **superseded** and not part of the design. |
| D2 | "Per region" | Per **marketplace** (US, UK, DE, FR, IT, ES). One refresh and one failure boundary per marketplace. |
| D3 | Target database | Existing Azure SQL `DAYHANSA_SQL1`. Varun has the required rights. |
| D4 | Scheduler | Runs on the team's **VM**. Platform details in D11. |
| D5 | Duplicate forecast pull | If the same generation date is pulled again, the content is compared with the stored version. Identical: nothing is written ("no new version", status `no_data`). Different: the new pull **replaces** the stored version, keeping the same `forecast_run_id`. |
| D6 | Credentials | Krishna shares the `.env` with Varun and Vivek for development. Key Vault is not part of this phase. |
| D7 | ASIN to product mapping | Uses the existing `dbo.amazon_asin_sku_map` table. The mapping stays out of the fact tables (section 7.2). |
| D8 | Both grains are kept | **New.** Closed months keep both their WEEK rows and their MONTH row. A week belongs to the month in which its **Saturday** falls. A straddling week (for example Sep 27 to Oct 3) belongs to October. Weekly and monthly totals for the same month will differ slightly. This is expected. |
| D9 | Never sum across period types | **New.** `period_type` is `WEEK` or `MONTH`. `DAY` is not used. Consumers read through views that return one grain at a time (section 7.3). A plain `SUM` over the base tables double-counts. |
| D10 | No-data behaviour | **New.** "Nothing new" means one of: no week in the window is closed yet; Amazon reports the period as not available; or the load inserts 0 rows and updates 0 rows. A run that only updated existing rows is `success`. If there is nothing new for a marketplace (for example the newest week is not yet published), the job logs it, writes an `amazon_ingest_run` row with status `no_data`, changes nothing, deletes nothing, and exits 0. Genuine errors still fail with exit code 1: authentication failure, a 4xx other than "not available", 429 or 5xx after retries, or a parse failure. The code must not treat every HTTP error as "not published yet". Genuine errors never become `no_data`. |
| D11 | Schedule and platform | **New.** The VM is **Ubuntu 24.04** (UK South, `vm-daylight-agents-01`), time zone UTC. Bash wrapper scripts and cron, not PowerShell and Task Scheduler. Both jobs run on **Wednesday**. Same-day retry on failure. No Thursday forecast run. Details in section 9.5. PowerShell instructions remain only for developer machines (section 6 and Appendix B). |
| D12 | Forecast | **New.** Weekly, one run per week. Versioned by `(marketplace, selling_program, forecast_generation_date)`. Replace-on-duplicate (D5). Raw JSON archived. One-time import of the US 2026-09-27 snapshot. A missed week cannot be recovered, so the wrapper retries within the same day, and the stale-version check (latest generation date older than 9 days) is the alert. Details in section 8. |

## 4. Scope

**In scope**

- Historical pull for sales and inventory (36 months, MONTH rows) and a first forecast pull, all six marketplaces.
- **Weekly history backfill** (WEEK rows) for sales and inventory, as far back as Amazon's weekly lookback allows. The limit is to be confirmed by the probe (T0).
- Database tables, views and load logic for sales, inventory, forecast versions, and an ingest run log.
- Weekly scheduled refresh of sales and inventory, including **WEEK rows for the previous and current month** (D1).
- Weekly scheduled, versioned forecast load.
- **Migration** of the two tables loaded on 2026-10-11 (`dbo.Daylight_sales_report`, `dbo.Daylight_inventory_report`) to the spec names `dbo.amazon_vendor_sales` and `dbo.amazon_vendor_inventory`, with a backup first (section 7.6).
- Retry, error handling, exit codes, logging, data quality checks, tests, and a runbook for the VM.

**Out of scope for this phase**

- Daily (DAY) report data, and month-to-date figures built from daily data.
- Azure Key Vault and managed identity.
- Power BI datasets and dashboards.
- Changes to the `amazon-demand-forecast` agent.
- Row-level history of restated values (we log how many rows changed per run, nothing more).

## 5. Amazon API facts the design depends on

**Monthly reports and forecast (verified)**

| Fact | Consequence |
|---|---|
| A monthly report window must end on a fully closed calendar month, and the data is only ready about 72 hours after the month closes. Otherwise the report fails with status `FATAL`. | The code requests the MONTH row only for closed months, with a safety margin (section 9.2). |
| One request covers at most 15 calendar months. | A 36-month pull is split into 3 requests per marketplace per report (15 + 15 + 6). Handled by the existing chunking code. |
| Lookback is rolling: only about 36 months back from today. Older months fail with `FATAL`. | Always use `--months 36`. Never hard-code a start month. |
| Reports use Pacific Time for day and month boundaries. | Month totals can differ slightly from UTC-based expectations at the edges. Not a bug. |
| One marketplace per request. | The scripts already loop per marketplace. |
| The forecast report returns only Amazon's **latest** forecast. Past forecasts cannot be requested. | A missed weekly run is lost permanently. Hence versioning, alerting and raw JSON archiving (section 8). |
| Forecast shape (verified on the US snapshot): 48 weekly buckets per ASIN (`weeks_ahead` 0 to 47), Sunday to Saturday, week 0 starts on the generation date (a Sunday). Fields: mean, P70, P80, P90 units, decimal values. | Drives the forecast tables. |
| Report creation is throttled by Amazon. | Sales and inventory need the same 429 and 5xx retry the forecast script already has. |
| The Brand Analytics role on the app authorisation has been confirmed for **US only**. | Treat the other marketplaces as unconfirmed until their first full run succeeds (T0, T1). |

**Weekly reports (to be confirmed by probe, T0)**

Nothing below is assumed. Each item must be answered by the read-only probe (T0) before the weekly code is finalised. Record the answers here.

| Item to confirm | Answer |
|---|---|
| Does the **inventory** report accept `reportPeriod = WEEK` for this account? | To be confirmed by probe (T0) |
| Publishing lag: how many days after a Saturday is that week available? | To be confirmed by probe (T0) |
| Maximum window per WEEK request (number of weeks) | To be confirmed by probe (T0) |
| Weekly lookback limit (how far back WEEK rows can be requested) | To be confirmed by probe (T0) |
| Is the weekly inventory snapshot taken at the start or the end of the week? | To be confirmed by probe (T0) |
| Which inventory fields are populated for WEEK rows? | To be confirmed by probe (T0) |
| How `ALL` rows behave in weekly reports (present per week, totals) | To be confirmed by probe (T0) |
| How Amazon signals "this week is not available yet" (status, error text), so it can be told apart from a genuine error (D10) | To be confirmed by probe (T0) |

Data observations from existing files:

- Dates are ISO `YYYY-MM-DD`. A monthly row has `start_date` = first day and `end_date` = last day of the month.
- Each marketplace's output starts with `asin = ALL` rows (account-level totals, one per month), followed by per-ASIN rows.
- Months with no activity still come back as rows of zeros with a currency (for example DE in late 2023). **Zero rows are valid data and must be stored, not dropped.**
- Some inventory fields come back empty. Observed: `open_po_units`, `unhealthy_*` and `sell_through_rate` are NULL for Sep 2023 to Feb 2024 in DE. NULL means "not supplied by Amazon", not zero.
- Currency by marketplace: US = USD, UK = GBP, DE/FR/IT/ES = EUR.
- Units are whole numbers. Revenue and cost have two decimals. `sell_through_rate` is a fraction with 4 decimals (for example 0.3653).

---

## 6. Requirement 1 — Historical backfill

### 6.1 What it delivers

CSV files for sales and inventory, covering the last 36 closed months (MONTH rows) and the available weekly history (WEEK rows) for each of the six marketplaces, plus one forecast snapshot per marketplace. These CSVs are used to validate the data and then load the database (requirement 2). CSVs and raw JSON are git-ignored and are never committed.

### 6.2 Procedure — monthly history (developer machine, Windows PowerShell)

These commands are for a developer's Windows machine. The VM uses bash (section 9.5).

1. Clone the repo, check out the working branch, and go to `agents/amazon-sp-api-ingest`.
2. Create the virtual environment and install dependencies:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. Place the `.env` Krishna shares into this folder. It must contain `SPAPI_CLIENT_ID`, `SPAPI_CLIENT_SECRET`, `SPAPI_CLIENT_ID_UK`, `SPAPI_CLIENT_SECRET_UK`, and `SPAPI_REFRESH_TOKEN_US`, `_UK`, `_DE`, `_FR`, `_IT`, `_ES`. If the file still has `SPAPI_REFRESH_TOKEN_NA`, rename it to `SPAPI_REFRESH_TOKEN_US`.
4. Check that every credential pair is accepted before the long run:
   ```powershell
   python probe_token_matrix.py
   ```
5. Run the backfill. **`--months 36` is the required form**, because it always requests the maximum valid window and never goes stale:
   ```powershell
   New-Item -ItemType Directory -Force output | Out-Null

   python sp_api_sales_report.py     --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_sales_report_{mp}.csv"
   python sp_api_inventory_report.py --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_inventory_report_{mp}.csv"
   python sp_api_forecast_report.py  --marketplaces US,UK,DE,FR,IT,ES --out-dir output/forecast_snapshots
   ```
   Quote `--out` so PowerShell does not interpret `{mp}`. The `{mp}` placeholder produces one CSV per marketplace.

### 6.3 Procedure — weekly history (developer machine, Windows PowerShell)

This runs after the probe (T0) has answered the weekly questions in section 5. It mirrors the monthly procedure.

- **Window:** as far back as the weekly lookback allows. To be set after the probe.
- **Chunk size:** weeks per request. To be set after the probe.
- Steps 1 to 4 are the same as section 6.2.
- Run the sales and inventory scripts with the weekly option (Proposed flag `--period WEEK`; exact flags to be set in T3) and write to separate files, for example `output/daylight_sales_report_week_{mp}.csv` and `output/daylight_inventory_report_week_{mp}.csv`.
- If the inventory report does not accept WEEK for this account (probe answer), the weekly history is sales only. Record this in section 5.
- Failure handling is the same as section 6.5: re-run only the failed marketplace. If the oldest chunk fails, shorten the window by one chunk.

### 6.4 What to expect (monthly history)

- As of 2026-10-06 the last closed month is September 2026, so `--months 36` covers **October 2023 to September 2026**, requested as three chunks: Oct 2023 to Dec 2024, Jan 2025 to Mar 2026, Apr 2026 to Sep 2026.
- Request count: 6 marketplaces x 3 chunks x 2 reports = 36 report requests for sales and inventory, plus 6 for the forecast. The run is sequential. Allow one to two hours and keep the terminal open.
- Raw JSON files (`raw_report_*.json`, `raw_inventory_*.json`) appear in the working folder. They are git-ignored and useful for debugging.

### 6.5 Failure handling

- A marketplace with missing credentials is skipped with a message naming the variable.
- A failed chunk prints `WARNING ... MISSING` with the marketplace and month. Re-run only the failed marketplace, for example `--marketplaces UK`. Each marketplace's CSV is overwritten independently.
- If the **oldest** chunk returns `FATAL`, the rolling lookback boundary has moved or is stricter than expected for that marketplace. Re-run with `--months 35`.
- A `429` (throttled) error means wait a few minutes and re-run that marketplace.

### 6.6 Acceptance criteria

For each of the 6 marketplaces and for each of sales and inventory:

- CSV exists, no `MISSING` warning.
- Monthly: **at least 36 distinct months, ending Sep 2026**, and one `ALL` row per month.
- Weekly: every week in the backfill window is present, with no gaps. Expected count to be set after the probe.
- Currency is the expected one (US USD, UK GBP, others EUR) on every row.
- Spot-check two months of the `ALL` row (`shipped_units`, `shipped_revenue`) against Vendor Central's Business Reports UI. Small differences are possible because of the Pacific-time boundary. Anything larger than about 1% needs investigation.
- One forecast snapshot per marketplace with 48 distinct `weeks_ahead` values per ASIN. UK and the four EU marketplaces have never run the forecast report. Compare the first raw JSON with the CSV before trusting it, as was done for US.

**Known difference.** The rows already in the database (loaded 2026-10-11) start at **2023-09-01 for DE**, one month earlier than a pull made today would reach. The likely reason (unverified): the backfill ran in September 2026 with `--start-month 2023-09`, when Sep 2023 was still inside Amazon's lookback. **The Sep 2023 rows are kept; rows are never deleted.** Run the check below per marketplace. It shows the first and last month and the month count. Any marketplace with fewer than 36 months, or not ending Sep 2026, must be explained in writing or reloaded.

```sql
-- Monthly coverage per marketplace (run on the old table now, and on the new table after T5)
SELECT marketplace,
       MIN(start_date)                    AS first_month,
       MAX(start_date)                    AS last_month,
       COUNT(DISTINCT start_date)         AS distinct_months
FROM dbo.Daylight_sales_report            -- after migration: dbo.amazon_vendor_sales, using period_start
GROUP BY marketplace                      -- and WHERE period_type = 'MONTH'
ORDER BY marketplace;
-- Expect: last_month 2026-09-01 and distinct_months >= 36 (first_month 2023-09-01 or 2023-10-01).
```

---

## 7. Requirement 2 — Database data model

**Decision:** tables live in schema `dbo` of `DAYHANSA_SQL1` and use the existing naming style (`amazon_...`, matching `dbo.amazon_asin_sku_map`).

### 7.1 Tables

| Table | Grain | Purpose |
|---|---|---|
| `dbo.amazon_ingest_run` | one row per job run per marketplace | Audit log: what ran, when, status, row counts |
| `dbo.amazon_vendor_sales` | marketplace x ASIN x period (`WEEK` or `MONTH`) | Sales fact data, including `ALL` aggregate rows |
| `dbo.amazon_vendor_inventory` | marketplace x ASIN x period (`WEEK` or `MONTH`) | Inventory fact data, including `ALL` aggregate rows |
| `dbo.amazon_forecast_run` | one row per forecast version | Version header |
| `dbo.amazon_forecast_weekly` | version x ASIN x week | Forecast detail (48 weeks per ASIN) |

Views (each returns **one grain only**, excludes `ALL` rows, and is joined to the SKU map):

- `dbo.vw_amazon_vendor_sales_weekly_sku`
- `dbo.vw_amazon_vendor_sales_monthly_sku`
- `dbo.vw_amazon_vendor_inventory_weekly_sku`
- `dbo.vw_amazon_vendor_inventory_monthly_sku`
- `dbo.vw_amazon_forecast_versioned` (all versions with `version_no` and `is_latest`)

These replace the single `vw_amazon_vendor_sales_sku` and `vw_amazon_vendor_inventory_sku` views of the earlier draft.

### 7.2 Design rules

- Natural keys are the primary keys. Loads use `MERGE`, so any load can be repeated safely (idempotent).
- **The loader never deletes rows.** A row is updated only if one of its measures actually changed.
- `period_type` is `WEEK` or `MONTH`. `DAY` is not used. It is part of the key `(marketplace, asin, period_type, period_start)`.
- > **Warning: never sum across period types.** The base tables hold both WEEK and MONTH rows for the same dates (D8). A plain `SUM` over `amazon_vendor_sales` or `amazon_vendor_inventory` **double-counts**. Always read through the views, which return one grain at a time (D9). Appendix C has a query that shows the overlap.
- A week belongs to the month in which its Saturday falls (D8). The weekly views expose this as `belongs_to_month`.
- `ALL` rows are stored in the same tables with `asin = 'ALL'`. A computed `is_aggregate` column marks them. **`ALL` rows are for validation only and must never feed demand planning.** The `_sku` views exclude them.
- **Inventory is a point-in-time snapshot. Never sum inventory across weeks or months.**
- **NULL means "not supplied by Amazon", not zero.** This applies especially to `open_po_units`, `unhealthy_units`, `unhealthy_cost` and `sell_through_rate`. Downstream code must treat NULL as unknown and must not replace it with 0.
- Never sum across marketplaces without converting currency.
- Timestamps are UTC (`SYSUTCDATETIME()`). Amazon dates are stored as `DATE` exactly as received.
- **Mapping stays out of the fact tables (D7).** The fact tables are an exact copy of what Amazon sent. The ASIN-to-product mapping is maintained by Daylight and changes over time. Joining at read time means a fix to the map corrects all history at once, with no reload.
- Joins to `amazon_asin_sku_map` are always **LEFT JOINs on both `asin` and `market`**. The same ASIN maps to different product codes per market (for example `B002ZOEVEA`: `E53047` in DE and ES, `D53047` in UK). An inner join would silently drop unmapped ASINs; a join on `asin` alone would multiply rows.
- DDL is run **by hand** by someone with database rights, never by application code.
- Scripts must be re-runnable (`IF OBJECT_ID(...) IS NULL`, `CREATE OR ALTER VIEW`).

### 7.3 DDL (Proposed — put in `sql/001_...` numbered scripts)

```sql
-- 001: run log
IF OBJECT_ID('dbo.amazon_ingest_run','U') IS NULL
CREATE TABLE dbo.amazon_ingest_run (
    run_id        BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_amazon_ingest_run PRIMARY KEY,
    job_name      VARCHAR(30)   NOT NULL,   -- 'sales' | 'inventory' | 'forecast'
    marketplace   CHAR(2)       NOT NULL,
    trigger_type  VARCHAR(20)   NOT NULL,   -- 'scheduled' | 'manual' | 'backfill' | 'import'
    window_start  DATE          NULL,       -- sales/inventory: first period requested
    window_end    DATE          NULL,       -- sales/inventory: last period requested
    status        VARCHAR(20)   NOT NULL,   -- 'running' | 'success' | 'partial' | 'failed' | 'no_data'
    rows_read     INT           NULL,
    rows_inserted INT           NULL,
    rows_updated  INT           NULL,       -- existing rows whose values changed (restatements)
    started_at    DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_ingest_run_start DEFAULT SYSUTCDATETIME(),
    finished_at   DATETIME2(0)  NULL,
    message       NVARCHAR(2000) NULL,
    host_name     NVARCHAR(128) NULL
);

-- 002: sales
IF OBJECT_ID('dbo.amazon_vendor_sales','U') IS NULL
CREATE TABLE dbo.amazon_vendor_sales (
    marketplace      CHAR(2)       NOT NULL,   -- US UK DE FR IT ES
    asin             VARCHAR(10)   NOT NULL,   -- 'ALL' = account-level aggregate (validation only)
    period_type      VARCHAR(10)   NOT NULL,   -- 'WEEK' | 'MONTH'; set explicitly by the loader
    period_start     DATE          NOT NULL,   -- WEEK: Sunday; MONTH: 1st of month
    period_end       DATE          NOT NULL,   -- WEEK: Saturday; MONTH: last day of month
    ordered_revenue  DECIMAL(18,2) NULL,
    ordered_units    INT           NULL,
    shipped_revenue  DECIMAL(18,2) NULL,
    shipped_cogs     DECIMAL(18,2) NULL,
    shipped_units    INT           NULL,       -- sell-out signal used for demand planning
    customer_returns INT           NULL,
    currency         CHAR(3)       NULL,
    is_aggregate     AS (CASE WHEN asin = 'ALL' THEN 1 ELSE 0 END),
    first_loaded_at  DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_vendor_sales_fl DEFAULT SYSUTCDATETIME(),
    last_updated_at  DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_vendor_sales_lu DEFAULT SYSUTCDATETIME(),
    last_run_id      BIGINT        NOT NULL,   -- last run that inserted or changed this row
    CONSTRAINT PK_amazon_vendor_sales PRIMARY KEY (marketplace, asin, period_type, period_start),
    CONSTRAINT CK_amazon_vendor_sales_pt CHECK (period_type IN ('WEEK','MONTH')),
    CONSTRAINT FK_amazon_vendor_sales_run FOREIGN KEY (last_run_id) REFERENCES dbo.amazon_ingest_run(run_id)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_amazon_vendor_sales_asin' AND object_id=OBJECT_ID('dbo.amazon_vendor_sales'))
    CREATE INDEX IX_amazon_vendor_sales_asin ON dbo.amazon_vendor_sales (asin, marketplace, period_start);

-- 003: inventory
IF OBJECT_ID('dbo.amazon_vendor_inventory','U') IS NULL
CREATE TABLE dbo.amazon_vendor_inventory (
    marketplace        CHAR(2)       NOT NULL,
    asin               VARCHAR(10)   NOT NULL,
    period_type        VARCHAR(10)   NOT NULL,   -- 'WEEK' | 'MONTH'; set explicitly by the loader
    period_start       DATE          NOT NULL,
    period_end         DATE          NOT NULL,
    net_received_units INT           NULL,
    net_received_cost  DECIMAL(18,2) NULL,
    open_po_units      INT           NULL,       -- NULL = not supplied by Amazon
    sellable_units     INT           NULL,
    sellable_cost      DECIMAL(18,2) NULL,
    unsellable_units   INT           NULL,
    unsellable_cost    DECIMAL(18,2) NULL,
    sell_through_rate  DECIMAL(9,4)  NULL,       -- fraction, e.g. 0.3653; NULL = not supplied
    aged_90plus_units  INT           NULL,
    aged_90plus_cost   DECIMAL(18,2) NULL,
    unhealthy_units    INT           NULL,       -- NULL = not supplied by Amazon
    unhealthy_cost     DECIMAL(18,2) NULL,
    currency           CHAR(3)       NULL,
    is_aggregate       AS (CASE WHEN asin = 'ALL' THEN 1 ELSE 0 END),
    first_loaded_at    DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_vendor_inv_fl DEFAULT SYSUTCDATETIME(),
    last_updated_at    DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_vendor_inv_lu DEFAULT SYSUTCDATETIME(),
    last_run_id        BIGINT        NOT NULL,
    CONSTRAINT PK_amazon_vendor_inventory PRIMARY KEY (marketplace, asin, period_type, period_start),
    CONSTRAINT CK_amazon_vendor_inv_pt CHECK (period_type IN ('WEEK','MONTH')),
    CONSTRAINT FK_amazon_vendor_inv_run FOREIGN KEY (last_run_id) REFERENCES dbo.amazon_ingest_run(run_id)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_amazon_vendor_inventory_asin' AND object_id=OBJECT_ID('dbo.amazon_vendor_inventory'))
    CREATE INDEX IX_amazon_vendor_inventory_asin ON dbo.amazon_vendor_inventory (asin, marketplace, period_start);

-- 004: forecast versions
IF OBJECT_ID('dbo.amazon_forecast_run','U') IS NULL
CREATE TABLE dbo.amazon_forecast_run (
    forecast_run_id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_amazon_forecast_run PRIMARY KEY,
    marketplace              CHAR(2)      NOT NULL,
    selling_program          VARCHAR(10)  NOT NULL CONSTRAINT DF_amazon_forecast_run_sp DEFAULT 'RETAIL',
    forecast_generation_date DATE         NOT NULL,   -- Amazon's date; the version identity
    report_last_updated      DATE         NULL,       -- reportSpecification.lastUpdatedDate
    pulled_at                DATETIME2(0) NOT NULL CONSTRAINT DF_amazon_forecast_run_pa DEFAULT SYSUTCDATETIME(),
    asin_count               INT          NOT NULL,
    row_count                INT          NOT NULL,
    week_count               SMALLINT     NOT NULL,   -- expected 48
    ingest_run_id            BIGINT       NOT NULL,
    raw_file_path            NVARCHAR(500) NULL,      -- archived raw JSON on the VM
    CONSTRAINT UQ_amazon_forecast_run UNIQUE (marketplace, selling_program, forecast_generation_date),
    CONSTRAINT FK_amazon_forecast_run_ingest FOREIGN KEY (ingest_run_id) REFERENCES dbo.amazon_ingest_run(run_id)
);

IF OBJECT_ID('dbo.amazon_forecast_weekly','U') IS NULL
CREATE TABLE dbo.amazon_forecast_weekly (
    forecast_run_id BIGINT       NOT NULL,
    asin            VARCHAR(10)  NOT NULL,
    week_start      DATE         NOT NULL,   -- Sunday
    week_end        DATE         NOT NULL,   -- Saturday
    weeks_ahead     SMALLINT     NOT NULL,   -- 0..47
    mean_units      DECIMAL(12,2) NULL,
    p70_units       DECIMAL(12,2) NULL,
    p80_units       DECIMAL(12,2) NULL,
    p90_units       DECIMAL(12,2) NULL,
    CONSTRAINT PK_amazon_forecast_weekly PRIMARY KEY (forecast_run_id, asin, week_start),
    CONSTRAINT FK_amazon_forecast_weekly_run FOREIGN KEY (forecast_run_id) REFERENCES dbo.amazon_forecast_run(forecast_run_id)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name='IX_amazon_forecast_weekly_asin' AND object_id=OBJECT_ID('dbo.amazon_forecast_weekly'))
    CREATE INDEX IX_amazon_forecast_weekly_asin ON dbo.amazon_forecast_weekly (asin, week_start);
```

Views (script `005_views.sql`). Every fact view returns one grain, excludes `ALL` rows, and exposes `period_type`, `is_partial` and `loaded_through`:

- `loaded_through` = the latest `period_end` loaded for that marketplace and grain.
- `is_partial` (weekly views) = 1 when the month the week belongs to is still in progress, that is, its last Saturday has not been loaded yet. The weekly totals for that month are then incomplete.
- `is_partial` (monthly views) = always 0, because MONTH rows exist only for closed months.

```sql
CREATE OR ALTER VIEW dbo.vw_amazon_vendor_sales_weekly_sku AS
SELECT s.marketplace, s.asin, m.product_code, m.family, m.product_description,
       s.period_type, s.period_start, s.period_end,
       DATEFROMPARTS(YEAR(s.period_end), MONTH(s.period_end), 1) AS belongs_to_month,  -- D8: month of the Saturday
       CASE WHEN EOMONTH(s.period_end) = EOMONTH(lt.loaded_through)
             AND DATEADD(day, 7, lt.loaded_through) <= EOMONTH(lt.loaded_through)  -- another Saturday still due this month
            THEN 1 ELSE 0 END AS is_partial,
       lt.loaded_through,
       s.ordered_revenue, s.ordered_units, s.shipped_revenue, s.shipped_cogs,
       s.shipped_units, s.customer_returns, s.currency
FROM dbo.amazon_vendor_sales s
JOIN (SELECT marketplace, MAX(period_end) AS loaded_through
      FROM dbo.amazon_vendor_sales WHERE period_type = 'WEEK'
      GROUP BY marketplace) lt ON lt.marketplace = s.marketplace
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = s.asin AND m.market = s.marketplace
WHERE s.period_type = 'WEEK' AND s.asin <> 'ALL';
GO
CREATE OR ALTER VIEW dbo.vw_amazon_vendor_sales_monthly_sku AS
SELECT s.marketplace, s.asin, m.product_code, m.family, m.product_description,
       s.period_type, s.period_start, s.period_end,
       CAST(0 AS BIT) AS is_partial,
       lt.loaded_through,
       s.ordered_revenue, s.ordered_units, s.shipped_revenue, s.shipped_cogs,
       s.shipped_units, s.customer_returns, s.currency
FROM dbo.amazon_vendor_sales s
JOIN (SELECT marketplace, MAX(period_end) AS loaded_through
      FROM dbo.amazon_vendor_sales WHERE period_type = 'MONTH'
      GROUP BY marketplace) lt ON lt.marketplace = s.marketplace
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = s.asin AND m.market = s.marketplace
WHERE s.period_type = 'MONTH' AND s.asin <> 'ALL';
GO
-- vw_amazon_vendor_inventory_weekly_sku and vw_amazon_vendor_inventory_monthly_sku:
-- same pattern over dbo.amazon_vendor_inventory, with the inventory columns.
-- Reminder: inventory is a snapshot; consumers must not SUM it across periods.
GO
CREATE OR ALTER VIEW dbo.vw_amazon_forecast_versioned AS
SELECT r.marketplace, r.selling_program, r.forecast_generation_date,
       DENSE_RANK() OVER (PARTITION BY r.marketplace, r.selling_program
                          ORDER BY r.forecast_generation_date) AS version_no,
       CASE WHEN r.forecast_generation_date =
                 MAX(r.forecast_generation_date) OVER (PARTITION BY r.marketplace, r.selling_program)
            THEN 1 ELSE 0 END AS is_latest,
       w.asin, w.week_start, w.week_end, w.weeks_ahead,
       w.mean_units, w.p70_units, w.p80_units, w.p90_units
FROM dbo.amazon_forecast_run r
JOIN dbo.amazon_forecast_weekly w ON w.forecast_run_id = r.forecast_run_id;
```

Before building on the SKU map, confirm `(asin, market)` is unique in `dbo.amazon_asin_sku_map` (query in Appendix C). If it is not, the `_sku` views would multiply rows.

### 7.4 Field mapping (API to CSV to table)

**Sales** (`GET_VENDOR_SALES_REPORT`: `salesAggregate` and `salesByAsin`)

| API field | CSV column | Table column |
|---|---|---|
| (marketplace being pulled) | `marketplace` | `marketplace` |
| (report period requested: `WEEK` or `MONTH`) | `period_type` (new) | `period_type` |
| `asin` (absent on aggregate, written as `ALL`) | `asin` | `asin` |
| `startDate` / `endDate` | `start_date` / `end_date` | `period_start` / `period_end` |
| `orderedRevenue.amount` and `.currencyCode` | `ordered_revenue`, `currency` | `ordered_revenue`, `currency` |
| `orderedUnits` | `ordered_units` | `ordered_units` |
| `shippedRevenue.amount` | `shipped_revenue` | `shipped_revenue` |
| `shippedCogs.amount` | `shipped_cogs` | `shipped_cogs` |
| `shippedUnits` | `shipped_units` | `shipped_units` |
| `customerReturns` | `customer_returns` | `customer_returns` |

**Inventory** (`GET_VENDOR_INVENTORY_REPORT`: `inventoryAggregate` and `inventoryByAsin`)

| API field | Table column |
|---|---|
| (report period requested: `WEEK` or `MONTH`) | `period_type` |
| `netReceivedInventoryUnits` / `netReceivedInventoryCost` | `net_received_units` / `net_received_cost` |
| `openPurchaseOrderUnits` | `open_po_units` |
| `sellableOnHandInventoryUnits` / `...Cost` | `sellable_units` / `sellable_cost` |
| `unsellableOnHandInventoryUnits` / `...Cost` | `unsellable_units` / `unsellable_cost` |
| `sellThroughRate` | `sell_through_rate` |
| `aged90PlusDaysSellableInventoryUnits` / `...Cost` | `aged_90plus_units` / `aged_90plus_cost` |
| `unhealthyInventoryUnits` / `...Cost` | `unhealthy_units` / `unhealthy_cost` |

Inventory is a **point-in-time snapshot** for the period, not an additive flow. Do not sum inventory across weeks or months. A field Amazon leaves out is stored as NULL, never as 0.

**Forecast** (`GET_VENDOR_FORECASTING_REPORT`: `forecastByAsin`)

| API field | Table column |
|---|---|
| `forecastGenerationDate` | `amazon_forecast_run.forecast_generation_date` |
| `reportSpecification.lastUpdatedDate` | `amazon_forecast_run.report_last_updated` |
| `asin` | `amazon_forecast_weekly.asin` |
| `startDate` / `endDate` | `week_start` / `week_end` |
| (computed by the script) | `weeks_ahead` |
| `meanForecastUnits`, `p70ForecastUnits`, `p80ForecastUnits`, `p90ForecastUnits` | `mean_units`, `p70_units`, `p80_units`, `p90_units` |

### 7.5 Load pattern for sales and inventory (Proposed)

Load each marketplace in its own transaction through a staging temp table, then `MERGE`. **The loader never deletes rows**: there is no `WHEN NOT MATCHED BY SOURCE` clause and no `DELETE` statement.

1. Insert an `amazon_ingest_run` row with status `running`.
2. Bulk-insert the parsed rows (WEEK and MONTH, each with its `period_type`) into `#stage` (pyodbc with `fast_executemany = True`).
3. `MERGE` into the fact table on the natural key. Update only when something differs, using `EXISTS (SELECT t.cols EXCEPT SELECT s.cols)`, which treats NULLs correctly:
   ```sql
   MERGE dbo.amazon_vendor_sales AS t
   USING #stage AS s
      ON  t.marketplace = s.marketplace AND t.asin = s.asin
      AND t.period_type = s.period_type AND t.period_start = s.period_start
   WHEN MATCHED AND EXISTS (
        SELECT t.period_end, t.ordered_revenue, t.ordered_units, t.shipped_revenue,
               t.shipped_cogs, t.shipped_units, t.customer_returns, t.currency
        EXCEPT
        SELECT s.period_end, s.ordered_revenue, s.ordered_units, s.shipped_revenue,
               s.shipped_cogs, s.shipped_units, s.customer_returns, s.currency)
      THEN UPDATE SET period_end = s.period_end, ordered_revenue = s.ordered_revenue,
           /* ...all measures... */ last_updated_at = SYSUTCDATETIME(), last_run_id = @run_id
   WHEN NOT MATCHED THEN INSERT (...) VALUES (..., @run_id)
   OUTPUT $action INTO @actions;
   ```
4. Count `INSERT` and `UPDATE` actions from `@actions` into `rows_inserted` and `rows_updated`, set the run to `success`, commit.
5. If there was nothing new for the marketplace (D10: no closed week, period not available, or the MERGE inserted 0 and updated 0): set the run to `no_data`. Nothing in the fact table has changed.
6. On any error: roll back that marketplace, set the run to `failed` with the message, and continue with the next marketplace.

`rows_updated` is the signal for Amazon restating data. A value that is large and unexpected is worth investigating.

### 7.6 Migration of the 2026-10-11 tables (Proposed)

On 2026-10-11 two tables were loaded with different names and columns: `dbo.Daylight_sales_report` and `dbo.Daylight_inventory_report`. They are keyed on `(marketplace, asin, start_date)`, have no `period_type`, and the current month's row may be a month-to-date roll-up of daily data. They are to be replaced by the spec tables.

1. **Find the readers first.** Confirm that nothing reads the old names: Power BI datasets, other agents, saved queries. Record the answer in the PR.
2. **Take a copy** before anything else. Either export each table to CSV, or `SELECT * INTO dbo.Daylight_sales_report_bak_20261015 FROM dbo.Daylight_sales_report` (same for inventory). Check the row counts match.
3. Create the spec tables with the numbered DDL scripts (section 7.3). **DDL is run by hand**, never by application code.
4. Load the spec tables from the validated backfill CSVs with `load_csv_to_db.py` (T5), not by copying rows from the old tables. Month-to-date rows built from daily data must not be carried over.
5. Run the coverage check in section 6.6 on both old and new tables and explain any difference.
6. Drop the old tables by hand only after Samir signs off. Keep the backup copy until then.

---

## 8. Requirement 4 — Weekly forecast, stored as versions (Decision D12)

### 8.1 Behaviour

- One run per week (Wednesday, section 9.5). Each run pulls Amazon's latest forecast for each marketplace and stores it as one **version**. A version is identified by `(marketplace, selling_program, forecast_generation_date)`.
- History is **append-only**: a new generation date creates a new version and never touches old ones.
- **Decision D5:** if the pulled generation date already exists, compare the downloaded content with the stored version. If identical, write nothing (section 8.4). If different, replace that version. In one transaction: find the existing run row, delete its weekly rows, update the header (`pulled_at`, counts, `report_last_updated`), insert the new weekly rows. The `forecast_run_id` stays the same. This is the only place rows are deleted, and only for the version being replaced.
- Each version holds about 48 weeks x about 75 to 80 ASINs, which is roughly 3,500 to 4,000 rows per marketplace per run. At six marketplaces and 52 runs a year that is about 1.2 million rows a year (estimate). Keep all versions; no purge.

### 8.2 Load transaction (Proposed)

```sql
BEGIN TRAN;
  SELECT @id = forecast_run_id FROM dbo.amazon_forecast_run WITH (UPDLOCK, HOLDLOCK)
   WHERE marketplace = @mp AND selling_program = @sp AND forecast_generation_date = @gen;

  IF @id IS NULL
  BEGIN
     INSERT dbo.amazon_forecast_run (...) VALUES (...);
     SET @id = SCOPE_IDENTITY();
  END
  ELSE
  BEGIN
     DELETE dbo.amazon_forecast_weekly WHERE forecast_run_id = @id;
     UPDATE dbo.amazon_forecast_run SET pulled_at = SYSUTCDATETIME(), asin_count = @n, ... WHERE forecast_run_id = @id;
  END

  -- bulk insert the weekly rows for @id
COMMIT;
```

### 8.3 Validation after each load

Warn (do not fail) if:

- `week_count` is not 48, or any ASIN does not have contiguous `weeks_ahead` 0 to 47.
- `p70 <= p80 <= p90` is violated, or any value is negative.
- The report contains no rows (treat as failed, and keep the raw JSON).

### 8.4 Stale-version detection

If the pulled generation date equals a stored one, compare the downloaded rows with the stored version:

- **Identical:** Amazon has not published a new forecast yet. This is not an error. Log "no new version", status `no_data`, exit 0. Nothing is written.
- **Different under the same date:** replace the version per D5 (same `forecast_run_id`), status `success`, and note "replaced" in the run message.
 There is no second scheduled run. A missed week cannot be recovered, so the wrapper retries failed runs within the same day (section 9.5), and the **stale-version check is the alert**: alert if a marketplace's latest generation date is more than 9 days old (query in Appendix C).

### 8.5 Raw archive

Because a missed week cannot be recovered, save every raw forecast JSON to `archive/forecast/raw_forecast_<MP>_<YYYYMMDD>.json` on the VM (gitignored) and store the path in `raw_file_path`. This allows a version to be rebuilt without Amazon.

### 8.6 One-time import

The US snapshot generated on 2026-09-27 exists on Krishna's machine (`forecast_snapshots/raw_forecast_US_20260927.json`). Load it once as version 1 for US with `trigger_type = 'import'`, so the history starts one week earlier. The loader must accept a raw JSON file path for this.

---

## 9. Requirement 3 — Weekly sales and inventory refresh, plus the engineering design shared by requirements 3 and 4

Sections 9.1 and 9.2 are specific to the sales and inventory refresh. Sections 9.3 to 9.5 (code changes, configuration, scheduling) apply to both weekly jobs, including the forecast job in section 8.

### 9.1 Behaviour (Decisions D1, D2, D8, D10)

Each weekly run, for each marketplace, for sales and for inventory:

1. **Work out the WEEK window.**
   - Start: the Sunday on or before the 1st of the previous calendar month.
   - End: the latest closed Saturday that Amazon has published. The publishing lag is a named constant, set after the probe (T0).
   - This covers every week whose Saturday falls in the previous or the current month (D8).
2. **Request WEEK rows** for the whole window and upsert them. The whole window is re-pulled every run, to catch restatements. Unchanged rows are not touched.
3. **Request the MONTH row for the previous month**, if the day-5 safety margin allows it (section 9.2). Request it on every run while it is the previous month, even if already stored, so restatements are caught. If Amazon says it is not available yet, skip it. This is not an error; the WEEK rows cover that month and the next run picks the MONTH row up.
4. **Record the outcome** in `amazon_ingest_run`:
   - `success`: at least one row was inserted or updated.
   - `no_data`: nothing new for this marketplace (D10): no closed week in the window, Amazon reports the period not available, or the load inserted 0 and updated 0. Nothing is changed or deleted. Exit code 0.
   - `partial`: one request succeeded and another hit a genuine error.
   - `failed`: a genuine error (authentication, a 4xx other than "not available", 429 or 5xx after retries, parse failure).

Other rules:

- Each marketplace is processed independently: its own run row, its own transaction, its own failure.
- Re-running is always safe (idempotent upsert, no deletes).
- "Not available" must be recognised by Amazon's specific response for that case (to be confirmed by probe, T0). Any other error is a failure.
- Dashboards show the in-progress month from the weekly views, labelled with `is_partial` and `loaded_through`.

Worked example (run on Wednesday 2026-10-21, assuming the newest published Saturday is 2026-10-17):

| Request | Window |
|---|---|
| WEEK | Sunday 2026-08-30 to Saturday 2026-10-17 (the Saturdays 5 Sep to 26 Sep belong to September, 3 Oct to 17 Oct to October) |
| MONTH | September 2026 (day 21 is past the day-5 margin) |

### 9.2 Month availability safety margin (code change)

`last_closed_month()` currently returns the previous calendar month regardless of the day. Amazon needs about 72 hours after month end (plus the Pacific-time offset). Change it so that if today (UTC) is on day **5 or earlier** of a month, the previous month is treated as not yet available.

The margin now decides **only** whether the previous month's MONTH report is requested. It does **not** block WEEK rows. It also applies to the monthly backfill. The constant must be a named, documented value: `MONTH_SAFETY_DAY = 5`.

### 9.3 Code changes (Proposed)

1. **Extract a shared module** `sp_api_common.py` containing credential resolution, token exchange, report create/poll/download, month and week chunking, the new retry helper, and the CSV writer. The three scripts and the probes import from it (today they import from `sp_api_sales_report.py`).
2. **Retry with backoff** for create, poll and document download: on HTTP 429, 500, 502, 503, 504 wait 30s, 60s, 120s, then fail (the forecast script's approach), applied to all three report types.
3. **Weekly period support** in the sales and inventory scripts (WEEK window from section 9.1, chunking from the probe results). The weekly job must **not** use the daily (DAY) logic from `sp_api_month_to_date.py`.
4. **Output sink flag** on the three report scripts: `--sink csv|db|both`. Default `csv` so nothing changes for existing usage. The scheduled jobs use `--sink db`.
5. **Dry run**: `--dry-run` shows what would be requested and loaded. It must work **without database credentials**.
6. **Database module** `db.py`: connection from environment, run-log helpers, the sales/inventory `MERGE` loader, the forecast version loader.
7. **CSV loader** `load_csv_to_db.py` for the backfill: reads the validated CSVs from `output/` and upserts them, so the backfill does not hit Amazon a second time. Also loads a raw forecast JSON file (section 8.6).
8. **Exit codes**: `0` all marketplaces succeeded or had no new data (`no_data`); `1` at least one marketplace or chunk failed (successful marketplaces are still committed); `2` nothing could run (for example no credentials). Today a partial failure prints a warning and still exits `0`, which a scheduler cannot detect.
9. **Logging**: scripts keep printing to stdout; the wrapper writes it to `logs/`. Never print secret values (the existing `describe_credentials` prints only lengths and the last six characters of the client id; keep it that way).

### 9.4 Configuration (add to `.env.example`)

```
# Database (Azure SQL DAYHANSA_SQL1)
DB_SERVER=
DB_NAME=
DB_USER=
DB_PASSWORD=
DB_DRIVER=ODBC Driver 18 for SQL Server
DB_PORT=1433
```

These are the names the demand-planning agent already uses (`agents/demand-planning/config.py`). Decided 2026-10-15; closes O6.

New dependencies: `pyodbc`. The machine running the code needs **ODBC Driver 18 for SQL Server**, and its public IP must be allowed in the Azure SQL firewall (both the VM and each developer's machine).

Use a dedicated SQL login for the VM job with rights only on the `amazon_*` tables and views, not the admin login.

### 9.5 Scheduling on the VM (Decisions D4, D11)

**Platform:** Ubuntu 24.04, `vm-daylight-agents-01`, UK South. VM time zone **UTC**. Bash wrappers and cron.

**VM prerequisites**

- Python virtual environment in the agent folder (`.venv/bin/python`).
- unixODBC and **Microsoft ODBC Driver 18 for SQL Server** installed (packages `unixodbc` and `msodbcsql18`, from Microsoft's package repository for Ubuntu).
- The VM's outbound IP must already be on the Azure SQL firewall. Check this before the first scheduled run.
- `.env` in the agent folder, readable only by the account that runs cron.

**Schedule (Proposed times)**

| Job | When (UTC) | Wrapper |
|---|---|---|
| Sales and inventory refresh | Wednesday 06:00 | `run_weekly_sales_inventory.sh` |
| Forecast pull | Wednesday 07:00 | `run_weekly_forecast.sh` |

There is no Thursday forecast run. Each wrapper retries a failed run within the same day: up to 3 attempts, 15 minutes apart. Exit code `2` (nothing could run, for example missing credentials) is not retried. Replace-on-duplicate (D5) and the idempotent upsert make retries harmless.

Crontab entries (`crontab -e` for the service account):

```
# m h dom mon dow  command
0 6 * * 3  /path/to/agents/amazon-sp-api-ingest/run_weekly_sales_inventory.sh >> /path/to/agents/amazon-sp-api-ingest/logs/cron.log 2>&1
0 7 * * 3  /path/to/agents/amazon-sp-api-ingest/run_weekly_forecast.sh        >> /path/to/agents/amazon-sp-api-ingest/logs/cron.log 2>&1
```

Sketch of the wrapper (one per job; the flag that selects the weekly window is set in T3):

```bash
#!/usr/bin/env bash
# run_weekly_sales_inventory.sh
set -u
cd "$(dirname "$0")"
mkdir -p logs
PY=.venv/bin/python
MPS=US,UK,DE,FR,IT,ES
LOG="logs/sales_inventory_$(date -u +%Y%m%d).log"

exec 9>logs/.sales_inventory.lock
flock -n 9 || { echo "$(date -u -Is) already running" >> "$LOG"; exit 1; }

for attempt in 1 2 3; do
  echo "=== attempt $attempt $(date -u -Is)" >> "$LOG"
  timeout 3h "$PY" sp_api_sales_report.py     --weekly-refresh --marketplaces "$MPS" --sink db >> "$LOG" 2>&1; rc1=$?
  timeout 3h "$PY" sp_api_inventory_report.py --weekly-refresh --marketplaces "$MPS" --sink db >> "$LOG" 2>&1; rc2=$?
  if [ "$rc1" -eq 0 ] && [ "$rc2" -eq 0 ]; then exit 0; fi
  if [ "$rc1" -eq 2 ] || [ "$rc2" -eq 2 ]; then exit 2; fi   # not retryable
  [ "$attempt" -lt 3 ] && sleep 900
done
exit 1
```

Rules for both wrappers: logs go under `logs/`; any failure gives a non-zero exit code; a run is stopped after 3 hours; a lock prevents two copies running at once. Add `logs/` and `archive/` to `.gitignore`.

Alerting for this phase: the run table, the wrapper's exit code in `logs/cron.log`, and the stale-forecast query. Cron has no built-in failure status, so a notification channel (email or Teams webhook) is an open item (O3).

---

## 10. Work breakdown

Suggested split: Varun (database rights) takes T2, T4, T5, T8; Vivek takes T1, T3, T6, T7; T9 and T10 shared. T0, T11 and T12 to be assigned. Adjust as you see fit.

**Retired code.** Vivek's `sp_api_month_to_date.py` is retired; the daily approach is superseded by D1. `ingest_to_db.py` is to be rewritten to the spec loader (section 7.5): no `ensure_table` (DDL is run by hand), no deletes, no commits every 200 rows (one transaction per marketplace).

| ID | Task | Depends on | Done when |
|---|---|---|---|
| T0 | **Read-only probe** of WEEK reports for sales and inventory on one marketplace. Answer every "to be confirmed" item in section 5. No database writes. Raw output gitignored. No secrets printed. | credentials | Section 5 weekly table filled in and reviewed |
| T1 | Run the 36-month backfill and the first forecast pull (section 6.2) and validate | credentials | Section 6.6 criteria met for all 6 marketplaces |
| T2 | Write numbered DDL scripts in `sql/` (section 7.3) and apply them by hand to `DAYHANSA_SQL1`; check the SKU map key is unique | database access | Tables and views exist; scripts re-run without error |
| T3 | Refactor into `sp_api_common.py`; add retry, the month safety margin, the WEEK window and chunking, `--sink`, `--dry-run`, no-data handling and exit codes (sections 9.1 to 9.3) | T0 | Existing CLI behaviour unchanged with `--sink csv`; `--dry-run` works without database credentials; unit tests pass |
| T4 | Build `db.py`: connection, run log (including `no_data`), sales/inventory `MERGE` loader for WEEK and MONTH rows (no deletes), forecast version loader. Replaces `ingest_to_db.py`. | T2 | Loading the same data twice gives `rows_inserted = 0` and `rows_updated = 0` the second time; no `DELETE` on fact tables |
| T5 | Build `load_csv_to_db.py` and load the T1 CSVs; import the 2026-09-27 US forecast as version 1 | T1, T2, T4 | Table row counts equal CSV row counts per marketplace; spot-check values against the CSV |
| T6 | Bash wrappers (`run_weekly_sales_inventory.sh`, `run_weekly_forecast.sh`) with `--sink db`, exit codes, same-day retry and logs (section 9.5) | T3, T4 | A manual run writes run rows and data for all marketplaces; a forced failure in one marketplace does not stop the others and returns exit code 1; a "nothing new" run writes `no_data` and returns 0 |
| T7 | Schedule both jobs with cron on the Ubuntu VM and write the runbook (venv, unixODBC and ODBC Driver 18, firewall, crontab, how to re-run a marketplace, how to read the run log) | T6, VM access | Jobs ran unattended on at least two Wednesdays; runbook committed |
| T8 | Add the data quality checks and views (section 11); produce the unmapped ASIN report | T2, T5 | Checks run after each load and write findings to `message` in the run log |
| T9 | Unit and integration tests (section 11.2) | T3, T4 | Tests pass in CI or locally with one command |
| T10 | Update `README.md`, `.env.example`, `.gitignore`; fix README text that is now stale (including the month-to-date section) | all | A new developer can follow the README alone |
| T11 | **Migrate the 2026-10-11 tables** (section 7.6): confirm no readers, back up, create spec tables by hand, reload, compare coverage | T2, T5 | Backup exists with matching row counts; readers confirmed; coverage check explained; old tables dropped only after sign-off |
| T12 | **Weekly history backfill** (section 6.3) and load | T0, T3, T4 | Weekly coverage criteria in section 6.6 met; WEEK rows loaded for all marketplaces where Amazon supports them |

Git workflow: one feature branch and one pull request per task; no data files, `.env` or logs committed (check `git status` before every commit).

## 11. Testing and data quality

### 11.1 Data quality checks (run after every load; findings go to the run log, they do not block the load)

- **Coverage:** every requested month and week has an `ALL` row per marketplace.
- **Currency:** matches the marketplace (US USD, UK GBP, DE/FR/IT/ES EUR).
- **Unmapped ASINs:** ASINs in sales, inventory or forecast that have no row in `amazon_asin_sku_map` for that marketplace. Report the count and the shipped units affected. Do not drop them. It is worth checking whether FR and IT rows exist in the map at all; the rows seen so far cover US, DE, ES and UK.
- **Consistency (informational):** the sum of per-ASIN `shipped_units` against the `ALL` row. They may legitimately differ.
- **Partial flag and `loaded_through`:** `loaded_through` equals the latest `period_end` loaded for each marketplace and grain; `is_partial` is 1 only for weeks in the month still in progress.
- **Weekly versus monthly (informational):** for each closed month, compare the sum of WEEK `shipped_units` (weeks whose Saturday is in that month) with the MONTH row. Report the difference against a tolerance (value to be agreed; differences are expected, D8). Never fail the load on this.
- **NULL profile:** count NULLs per inventory field, per marketplace and per period type, so gaps like DE Sep 2023 to Feb 2024 are visible.
- **Forecast:** the checks in section 8.3.
- **Freshness:** the stale-version query (Appendix C).

### 11.2 Tests

- Unit: `month_window_chunks` (including the 36-month case giving 15 + 15 + 6), the day-5 safety margin, `_amount`, and the three `flatten_*` functions using small fixture JSON files (trimmed copies of real raw reports with ASINs and values anonymised).
- Unit: the WEEK window (start on the Sunday on or before the 1st of the previous month; a straddling week is assigned to the month of its Saturday), including a run in the first 5 days of a month.
- Unit: classification of Amazon responses into "not available" (`no_data`) versus genuine errors (D10).
- Retry helper: simulate 429 then success; simulate repeated failure.
- Database (against a separate test schema or database, never production tables): idempotent re-load; an update changes `rows_updated` and `last_updated_at`; a `no_data` run changes nothing; replacing a forecast version keeps the same `forecast_run_id` and replaces its rows; a failed marketplace rolls back without affecting the others.

---

## 12. Open items to confirm

O1 (VM operating system: Ubuntu 24.04, D11), O4 (schedule day: Wednesday, D11) and O6 (database variable names: `DB_*`, section 9.4) are answered and removed.

| # | Item | Default if not answered |
|---|---|---|
| O2 | Refresh window N (months re-pulled weekly) | Superseded by D1 (previous and current month of WEEK rows, plus previous MONTH row) |
| O3 | Alerting channel for failed or stale jobs | Run table, wrapper exit codes in `logs/`, and the stale-forecast query only |
| O5 | FR and IT coverage in `dbo.amazon_asin_sku_map` | Report them as unmapped until fixed |
| O7 | Dedicated least-privilege SQL login for the VM job (needs Varun) | Create one before T7 |
| O8 | Where raw forecast JSON is archived on the VM | `archive/forecast/` inside the agent folder |
| O9 | UK sales (only Aug 2026 tested) and the forecast report for UK and EU (never run) | Treat the first full run as a verification step in T1 |
| O10 | Probe results: weekly publishing lag, WEEK support for inventory, window and lookback limits (section 5) | Weekly code is not finalised until T0 is done |
| O11 | Does Samir need exact calendar "month so far" figures? A straddling week cannot be split without daily data. The design keeps weeks as weeks (D8). | Weeks as weeks |
| O12 | Samir sign-off on the 2026-10-15 revision | Pending |
| O13 | Tolerance for the weekly versus monthly reconciliation (section 11.1) | Report only, no threshold |
| O14 | The exact times for the Wednesday runs (section 9.5) | 06:00 and 07:00 UTC |

## Appendix A. Amazon constraints quick reference

- Marketplaces and endpoints: US uses the NA endpoint; UK, DE, FR, IT, ES use the EU endpoint.
- Sales and inventory request options: `reportPeriod = MONTH` or `reportPeriod = WEEK`, `distributorView = MANUFACTURING`, `sellingProgram = RETAIL`. WEEK limits, lag and inventory support are to be confirmed by probe (T0).
- Forecast request option: `sellingProgram` only. No date window.
- Required Vendor Central role on the app authorisation: Brand Analytics (confirmed for US only).

## Appendix B. Backfill command block (developer machine, Windows PowerShell)

For a developer's Windows machine only. The VM uses bash (section 9.5).

```powershell
cd agents\amazon-sp-api-ingest
.venv\Scripts\Activate.ps1
New-Item -ItemType Directory -Force output | Out-Null

python sp_api_sales_report.py     --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_sales_report_{mp}.csv"
python sp_api_inventory_report.py --months 36 --marketplaces US,UK,DE,FR,IT,ES --out "output/daylight_inventory_report_{mp}.csv"
python sp_api_forecast_report.py  --marketplaces US,UK,DE,FR,IT,ES --out-dir output/forecast_snapshots

# Weekly history (after T0 and T3; flags and window to be set then)
# python sp_api_sales_report.py     --period WEEK ... --out "output/daylight_sales_report_week_{mp}.csv"
# python sp_api_inventory_report.py --period WEEK ... --out "output/daylight_inventory_report_week_{mp}.csv"
```

## Appendix C. Useful queries

```sql
-- SKU map: confirm (asin, market) is unique (expect no rows)
SELECT asin, market, COUNT(*) AS n
FROM dbo.amazon_asin_sku_map GROUP BY asin, market HAVING COUNT(*) > 1;

-- ASINs with sales but no mapping
SELECT s.marketplace, s.asin, SUM(s.shipped_units) AS shipped_units
FROM dbo.amazon_vendor_sales s
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = s.asin AND m.market = s.marketplace
WHERE s.asin <> 'ALL' AND m.asin IS NULL
  AND s.period_type = 'MONTH'               -- one grain only, see D9
GROUP BY s.marketplace, s.asin
ORDER BY shipped_units DESC;

-- loaded_through per marketplace and period type
SELECT marketplace, period_type,
       MIN(period_start) AS first_period,
       MAX(period_end)   AS loaded_through,
       COUNT(DISTINCT period_start) AS periods
FROM dbo.amazon_vendor_sales
GROUP BY marketplace, period_type
ORDER BY marketplace, period_type;

-- Double-count risk: (marketplace, asin) with WEEK and MONTH rows covering the same dates.
-- Every closed month should appear here (D8). It shows why a plain SUM over the table is wrong.
SELECT w.marketplace, w.asin, m.period_start AS month_start,
       COUNT(*)             AS overlapping_weeks,
       MAX(m.shipped_units) AS month_shipped_units,
       SUM(w.shipped_units) AS overlapping_week_shipped_units
FROM dbo.amazon_vendor_sales m
JOIN dbo.amazon_vendor_sales w
  ON  w.marketplace = m.marketplace AND w.asin = m.asin
  AND m.period_type = 'MONTH' AND w.period_type = 'WEEK'
  AND w.period_start <= m.period_end AND w.period_end >= m.period_start
GROUP BY w.marketplace, w.asin, m.period_start
ORDER BY w.marketplace, w.asin, m.period_start;

-- Stale forecast: latest generation date older than 9 days
SELECT marketplace, MAX(forecast_generation_date) AS latest_generation,
       DATEDIFF(day, MAX(forecast_generation_date), CAST(SYSUTCDATETIME() AS date)) AS age_days
FROM dbo.amazon_forecast_run
GROUP BY marketplace
HAVING DATEDIFF(day, MAX(forecast_generation_date), CAST(SYSUTCDATETIME() AS date)) > 9;

-- How one week's forecast changed across versions
SELECT version_no, forecast_generation_date, weeks_ahead, mean_units, p80_units
FROM dbo.vw_amazon_forecast_versioned
WHERE marketplace = 'US' AND asin = 'B0052OTB6S' AND week_start = '2026-10-25'
ORDER BY forecast_generation_date;

-- Monthly shipped units by Daylight product code (one grain: monthly view)
SELECT marketplace, product_code, period_start, SUM(shipped_units) AS shipped_units
FROM dbo.vw_amazon_vendor_sales_monthly_sku
GROUP BY marketplace, product_code, period_start
ORDER BY marketplace, product_code, period_start;

-- Weekly shipped units by product code, rolled to the month of the Saturday (D8)
SELECT marketplace, product_code, belongs_to_month, MAX(is_partial) AS is_partial,
       SUM(shipped_units) AS shipped_units
FROM dbo.vw_amazon_vendor_sales_weekly_sku
GROUP BY marketplace, product_code, belongs_to_month
ORDER BY marketplace, product_code, belongs_to_month;
```
