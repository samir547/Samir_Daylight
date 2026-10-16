-- ============================================================================
-- 008  FALLBACK ONLY: filtered copy of closed months from the 2026-10-11 tables
--      (data-ingestion-spec.md section 7.6 step 4, task T11)
--
-- The normal route is load_csv_to_db.py (T5), which loads the validated
-- monthly backfill CSVs as period_type 'MONTH'. Use this script ONLY if that
-- route is not possible, and only with sign-off.
--
-- Copies from dbo.Daylight_sales_report / dbo.Daylight_inventory_report into
-- dbo.amazon_vendor_sales / dbo.amazon_vendor_inventory as period_type
-- 'MONTH', keeping ONLY rows that are:
--   * a whole calendar month (start on the 1st, end on the last day), and
--   * closed at least MONTH_SAFETY_DAY days before the row was last loaded
--     (loaded on day 6 or later of the next month, the same rule as the
--     weekly job, spec 9.2), and
--   * not last written by sp_api_month_to_date.py (source_file), and
--   * a valid key for the spec tables (marketplace US UK DE FR IT ES,
--     ASIN at most 10 characters).
-- The old tables hold month-to-date roll-ups labelled 'final'; the second and
-- third rules leave those out (007_checks.sql, check 3b, lists them). The
-- third also leaves out genuine MONTH rows that script fetched; reload those
-- months from Amazon or the CSVs.
-- NEVER copy from the _bak_20261015 backups: they hold the same rows.
--
-- Insert-only: a key already in the spec table (for example loaded from the
-- CSVs) is never overwritten, and nothing is deleted. Each copied marketplace
-- gets an amazon_ingest_run row (trigger_type 'import') for last_run_id.
--
-- Requires 001-003. Run BY HAND (section 7.2). By default it only PREVIEWS
-- what it would copy and why rows are left out; set @apply = 1 to copy.
-- Re-runnable: a second apply inserts nothing and logs no_data. If an old
-- table lacks loaded_at or source_file the script fails before writing.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET XACT_ABORT ON;
SET NOCOUNT ON;
GO

DECLARE @apply BIT = 0;              -- 0 = preview only, 1 = copy
DECLARE @month_safety_day INT = 5;   -- MONTH_SAFETY_DAY (week_plan.py, spec 9.2)

IF OBJECT_ID('dbo.amazon_ingest_run', 'U') IS NULL OR OBJECT_ID('dbo.amazon_vendor_sales', 'U') IS NULL
   OR OBJECT_ID('dbo.amazon_vendor_inventory', 'U') IS NULL
    THROW 50008, 'Run 001-003 first: the spec tables do not exist.', 1;
IF OBJECT_ID('dbo.Daylight_sales_report', 'U') IS NULL OR OBJECT_ID('dbo.Daylight_inventory_report', 'U') IS NULL
    THROW 50008, 'dbo.Daylight_sales_report or dbo.Daylight_inventory_report does not exist; nothing to copy.', 1;

DROP TABLE IF EXISTS #sales;
DROP TABLE IF EXISTS #inventory;

-- ---- classify every old row: keep, or the first reason it is left out
SELECT CAST(marketplace AS NVARCHAR(10)) AS marketplace, asin, start_date, end_date,
       ordered_revenue, ordered_units, shipped_revenue, shipped_cogs, shipped_units,
       customer_returns, currency,
       CASE WHEN DAY(start_date) <> 1 OR end_date <> EOMONTH(start_date) THEN 'not_whole_month'
            WHEN source_file LIKE 'sp_api_month_to_date%'                 THEN 'month_to_date_script'
            WHEN CAST(loaded_at AS DATE) <= DATEADD(day, @month_safety_day, EOMONTH(start_date))
                                                                          THEN 'loaded_within_safety_days'
            WHEN marketplace NOT IN ('US', 'UK', 'DE', 'FR', 'IT', 'ES') OR LEN(asin) > 10
                                                                          THEN 'invalid_key'
            ELSE 'keep' END AS outcome
INTO #sales
FROM dbo.Daylight_sales_report;

SELECT CAST(marketplace AS NVARCHAR(10)) AS marketplace, asin, start_date, end_date,
       net_received_units, net_received_cost, open_po_units, sellable_units, sellable_cost,
       unsellable_units, unsellable_cost, sell_through_rate, aged_90plus_units, aged_90plus_cost,
       unhealthy_units, unhealthy_cost, currency,
       CASE WHEN DAY(start_date) <> 1 OR end_date <> EOMONTH(start_date) THEN 'not_whole_month'
            WHEN source_file LIKE 'sp_api_month_to_date%'                 THEN 'month_to_date_script'
            WHEN CAST(loaded_at AS DATE) <= DATEADD(day, @month_safety_day, EOMONTH(start_date))
                                                                          THEN 'loaded_within_safety_days'
            WHEN marketplace NOT IN ('US', 'UK', 'DE', 'FR', 'IT', 'ES') OR LEN(asin) > 10
                                                                          THEN 'invalid_key'
            ELSE 'keep' END AS outcome
INTO #inventory
FROM dbo.Daylight_inventory_report;

-- ---- preview: rows per marketplace and outcome, and keys already present
SELECT 'sales' AS report, c.marketplace, c.outcome, COUNT(*) AS row_count,
       MIN(c.start_date) AS first_month, MAX(c.start_date) AS last_month,
       SUM(CASE WHEN t.asin IS NOT NULL THEN 1 ELSE 0 END) AS already_in_spec_table
FROM #sales c
LEFT JOIN dbo.amazon_vendor_sales t
       ON c.outcome = 'keep' AND t.marketplace = c.marketplace AND t.asin = c.asin
      AND t.period_type = 'MONTH' AND t.period_start = c.start_date
GROUP BY c.marketplace, c.outcome
UNION ALL
SELECT 'inventory', c.marketplace, c.outcome, COUNT(*),
       MIN(c.start_date), MAX(c.start_date),
       SUM(CASE WHEN t.asin IS NOT NULL THEN 1 ELSE 0 END)
FROM #inventory c
LEFT JOIN dbo.amazon_vendor_inventory t
       ON c.outcome = 'keep' AND t.marketplace = c.marketplace AND t.asin = c.asin
      AND t.period_type = 'MONTH' AND t.period_start = c.start_date
GROUP BY c.marketplace, c.outcome
ORDER BY report, marketplace, outcome;

IF @apply = 0
BEGIN
    PRINT 'Preview only: nothing written. Set @apply = 1 to copy the rows with outcome = keep.';
    RETURN;
END

-- ---- copy, one transaction for both tables
DECLARE @runs TABLE (job_name VARCHAR(30), marketplace CHAR(2), run_id BIGINT);
DECLARE @ins  TABLE (job_name VARCHAR(30), marketplace CHAR(2));

BEGIN TRANSACTION;

    INSERT dbo.amazon_ingest_run (job_name, marketplace, trigger_type, window_start, window_end,
                                  status, rows_read, message, host_name)
    OUTPUT inserted.job_name, inserted.marketplace, inserted.run_id INTO @runs
    SELECT 'sales', marketplace, 'import', MIN(start_date), MAX(start_date), 'running', COUNT(*),
           '008 fallback copy from dbo.Daylight_sales_report (closed whole months only)', HOST_NAME()
    FROM #sales WHERE outcome = 'keep' GROUP BY marketplace
    UNION ALL
    SELECT 'inventory', marketplace, 'import', MIN(start_date), MAX(start_date), 'running', COUNT(*),
           '008 fallback copy from dbo.Daylight_inventory_report (closed whole months only)', HOST_NAME()
    FROM #inventory WHERE outcome = 'keep' GROUP BY marketplace;

    INSERT dbo.amazon_vendor_sales (marketplace, asin, period_type, period_start, period_end,
                                    ordered_revenue, ordered_units, shipped_revenue, shipped_cogs,
                                    shipped_units, customer_returns, currency, last_run_id)
    OUTPUT 'sales', inserted.marketplace INTO @ins
    SELECT c.marketplace, c.asin, 'MONTH', c.start_date, c.end_date,
           c.ordered_revenue, c.ordered_units, c.shipped_revenue, c.shipped_cogs,
           c.shipped_units, c.customer_returns, c.currency, r.run_id
    FROM #sales c
    JOIN @runs r ON r.job_name = 'sales' AND r.marketplace = c.marketplace
    WHERE c.outcome = 'keep'
      AND NOT EXISTS (SELECT 1 FROM dbo.amazon_vendor_sales t
                      WHERE t.marketplace = c.marketplace AND t.asin = c.asin
                        AND t.period_type = 'MONTH' AND t.period_start = c.start_date);

    INSERT dbo.amazon_vendor_inventory (marketplace, asin, period_type, period_start, period_end,
                                        net_received_units, net_received_cost, open_po_units,
                                        sellable_units, sellable_cost, unsellable_units, unsellable_cost,
                                        sell_through_rate, aged_90plus_units, aged_90plus_cost,
                                        unhealthy_units, unhealthy_cost, currency, last_run_id)
    OUTPUT 'inventory', inserted.marketplace INTO @ins
    SELECT c.marketplace, c.asin, 'MONTH', c.start_date, c.end_date,
           c.net_received_units, c.net_received_cost, c.open_po_units,
           c.sellable_units, c.sellable_cost, c.unsellable_units, c.unsellable_cost,
           c.sell_through_rate, c.aged_90plus_units, c.aged_90plus_cost,
           c.unhealthy_units, c.unhealthy_cost, c.currency, r.run_id
    FROM #inventory c
    JOIN @runs r ON r.job_name = 'inventory' AND r.marketplace = c.marketplace
    WHERE c.outcome = 'keep'
      AND NOT EXISTS (SELECT 1 FROM dbo.amazon_vendor_inventory t
                      WHERE t.marketplace = c.marketplace AND t.asin = c.asin
                        AND t.period_type = 'MONTH' AND t.period_start = c.start_date);

    UPDATE ir
    SET rows_inserted = n.inserted_rows,
        rows_updated  = 0,
        status        = CASE WHEN n.inserted_rows > 0 THEN 'success' ELSE 'no_data' END,
        finished_at   = SYSUTCDATETIME()
    FROM dbo.amazon_ingest_run ir
    JOIN @runs r ON r.run_id = ir.run_id
    CROSS APPLY (SELECT COUNT(*) AS inserted_rows FROM @ins i
                 WHERE i.job_name = r.job_name AND i.marketplace = r.marketplace) n;

COMMIT TRANSACTION;

SELECT ir.run_id, ir.job_name, ir.marketplace, ir.status, ir.rows_read, ir.rows_inserted,
       ir.window_start, ir.window_end
FROM dbo.amazon_ingest_run ir
JOIN @runs r ON r.run_id = ir.run_id
ORDER BY ir.job_name, ir.marketplace;
GO
