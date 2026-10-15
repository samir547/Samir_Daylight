-- ============================================================================
-- 007  Read-only checks for setup and migration
--      (data-ingestion-spec.md sections 6.6, 7.3, 7.6; tasks T2 and T11)
--
-- Changes nothing: SELECTs only. Safe to run at any time and as often as
-- needed. Each check skips itself (with a message) if its table does not
-- exist yet.
--
--   1. SKU map key: (asin, market) must be unique in dbo.amazon_asin_sku_map
--      before the _sku views are used (T2). Expect NO rows.
--   2. Spec objects present (after 001-005). Expect present = 1 everywhere.
--   3. Old tables: month-to-date rows. These must NOT be carried over (7.6
--      step 4); this only shows how many there are.
--   4. Monthly coverage per marketplace, old tables (whole months only).
--   5. Coverage per marketplace and period_type, spec tables (after T5).
--      Expect for MONTH: last_period 2026-09-01 (or later) and at least 36
--      periods (6.6). Compare with check 4 and explain any difference
--      (7.6 step 5); DE may start one month earlier (2023-09) in the old table.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
SET NOCOUNT ON;
GO

-- 1. SKU map: duplicate (asin, market) keys. Expect no rows.
IF OBJECT_ID('dbo.amazon_asin_sku_map', 'U') IS NULL
    PRINT 'Check 1 skipped: dbo.amazon_asin_sku_map does not exist.';
ELSE
    SELECT 'check 1: duplicate SKU map keys' AS check_name, asin, market, COUNT(*) AS n
    FROM dbo.amazon_asin_sku_map
    GROUP BY asin, market
    HAVING COUNT(*) > 1;
GO

-- 2. Spec objects present.
SELECT 'check 2: objects' AS check_name, o.name, o.kind,
       CASE WHEN OBJECT_ID('dbo.' + o.name, o.kind) IS NULL THEN 0 ELSE 1 END AS present
FROM (VALUES ('amazon_ingest_run', 'U'), ('amazon_vendor_sales', 'U'), ('amazon_vendor_inventory', 'U'),
             ('amazon_forecast_run', 'U'), ('amazon_forecast_weekly', 'U'),
             ('vw_amazon_vendor_sales_weekly_sku', 'V'), ('vw_amazon_vendor_sales_monthly_sku', 'V'),
             ('vw_amazon_vendor_inventory_weekly_sku', 'V'), ('vw_amazon_vendor_inventory_monthly_sku', 'V'),
             ('vw_amazon_forecast_versioned', 'V'),
             ('Daylight_sales_report_bak_20261015', 'U'), ('Daylight_inventory_report_bak_20261015', 'U'))
     AS o (name, kind)
ORDER BY o.kind, o.name;
GO

-- 3. Old tables: rows that are not a whole calendar month (month-to-date
--    roll-ups of daily data). Based on the dates, not period_status, which
--    older loads may not have.
IF OBJECT_ID('dbo.Daylight_sales_report', 'U') IS NULL
    PRINT 'Check 3 skipped: dbo.Daylight_sales_report does not exist.';
ELSE
    SELECT 'check 3: old sales month-to-date rows' AS check_name, marketplace,
           COUNT(*) AS rows_not_whole_month, MIN(start_date) AS first_start, MAX(end_date) AS last_end
    FROM dbo.Daylight_sales_report
    WHERE end_date <> EOMONTH(start_date)
    GROUP BY marketplace
    ORDER BY marketplace;

IF OBJECT_ID('dbo.Daylight_inventory_report', 'U') IS NULL
    PRINT 'Check 3 skipped: dbo.Daylight_inventory_report does not exist.';
ELSE
    SELECT 'check 3: old inventory month-to-date rows' AS check_name, marketplace,
           COUNT(*) AS rows_not_whole_month, MIN(start_date) AS first_start, MAX(end_date) AS last_end
    FROM dbo.Daylight_inventory_report
    WHERE end_date <> EOMONTH(start_date)
    GROUP BY marketplace
    ORDER BY marketplace;
GO

-- 4. Monthly coverage, old tables (whole months only).
IF OBJECT_ID('dbo.Daylight_sales_report', 'U') IS NULL
    PRINT 'Check 4 skipped: dbo.Daylight_sales_report does not exist.';
ELSE
    SELECT 'check 4: old sales coverage' AS check_name, marketplace,
           MIN(start_date) AS first_month, MAX(start_date) AS last_month,
           COUNT(DISTINCT start_date) AS distinct_months
    FROM dbo.Daylight_sales_report
    WHERE end_date = EOMONTH(start_date)
    GROUP BY marketplace
    ORDER BY marketplace;

IF OBJECT_ID('dbo.Daylight_inventory_report', 'U') IS NULL
    PRINT 'Check 4 skipped: dbo.Daylight_inventory_report does not exist.';
ELSE
    SELECT 'check 4: old inventory coverage' AS check_name, marketplace,
           MIN(start_date) AS first_month, MAX(start_date) AS last_month,
           COUNT(DISTINCT start_date) AS distinct_months
    FROM dbo.Daylight_inventory_report
    WHERE end_date = EOMONTH(start_date)
    GROUP BY marketplace
    ORDER BY marketplace;
GO

-- 5. Coverage, spec tables, one row per marketplace and grain (never mix
--    grains, D9). all_rows = periods that have an 'ALL' row; expect it to
--    equal periods (11.1, coverage).
IF OBJECT_ID('dbo.amazon_vendor_sales', 'U') IS NULL
    PRINT 'Check 5 skipped: dbo.amazon_vendor_sales does not exist.';
ELSE
    SELECT 'check 5: sales coverage' AS check_name, marketplace, period_type,
           MIN(period_start) AS first_period, MAX(period_start) AS last_period,
           COUNT(DISTINCT period_start) AS periods,
           COUNT(DISTINCT CASE WHEN asin = 'ALL' THEN period_start END) AS all_rows
    FROM dbo.amazon_vendor_sales
    GROUP BY marketplace, period_type
    ORDER BY marketplace, period_type;

IF OBJECT_ID('dbo.amazon_vendor_inventory', 'U') IS NULL
    PRINT 'Check 5 skipped: dbo.amazon_vendor_inventory does not exist.';
ELSE
    SELECT 'check 5: inventory coverage' AS check_name, marketplace, period_type,
           MIN(period_start) AS first_period, MAX(period_start) AS last_period,
           COUNT(DISTINCT period_start) AS periods,
           COUNT(DISTINCT CASE WHEN asin = 'ALL' THEN period_start END) AS all_rows
    FROM dbo.amazon_vendor_inventory
    GROUP BY marketplace, period_type
    ORDER BY marketplace, period_type;
GO
