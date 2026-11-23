-- ============================================================================
-- 007  Sanity warning stored with each run, and two Power BI views that show
--      the run-level picture and the ASINs that were not forecast.
--
-- Needs 005 and 006 applied first. Run BY HAND, in SSMS, on DAYHANSA_SQL1.
-- Re-runnable: the columns are added only if missing; the views use CREATE OR ALTER.
-- Nothing here changes an existing column or view. Until it is applied the
-- forecast still loads; the log says that the warning was not stored.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH('dbo.amazon_order_forecast_run_summary', 'sanity_status') IS NULL
    ALTER TABLE dbo.amazon_order_forecast_run_summary ADD sanity_status VARCHAR(10) NULL;     -- ok | warn | forced
GO
IF COL_LENGTH('dbo.amazon_order_forecast_run_summary', 'sanity_warning') IS NULL
    ALTER TABLE dbo.amazon_order_forecast_run_summary ADD sanity_warning VARCHAR(500) NULL;   -- plain-English text; NULL when ok
GO

-- One row per run: what happened, and whether it needs a person to look at it.
-- Power BI: filter is_latest = 1 for the current run of each market.
CREATE OR ALTER VIEW dbo.vw_amazon_order_forecast_run_status AS
SELECT  r.order_forecast_run_id, r.marketplace, r.method, r.train_end, r.created_at,
        CASE WHEN r.order_forecast_run_id = (SELECT TOP 1 r2.order_forecast_run_id
                                             FROM dbo.amazon_order_forecast_run r2
                                             WHERE r2.marketplace = r.marketplace
                                             ORDER BY r2.train_end DESC, r2.created_at DESC, r2.order_forecast_run_id DESC)
             THEN 1 ELSE 0 END AS is_latest,
        s.asins_in_sales, s.asins_forecast, s.young_product_backups, s.skipped_short_history, s.skipped_no_sales,
        s.asins_without_amazon_forecast, s.sales_only_asins, s.seasonality_mode, s.sanity_ratio,
        s.sanity_status, s.sanity_warning,
        CASE WHEN s.sanity_status IN ('warn', 'forced') THEN 'Review'
             WHEN s.sanity_status = 'ok' THEN 'OK'
             ELSE 'No check recorded' END AS review_flag
FROM    dbo.amazon_order_forecast_run r
LEFT JOIN dbo.amazon_order_forecast_run_summary s ON s.order_forecast_run_id = r.order_forecast_run_id;
GO

-- One row per ASIN per run, including the ASINs that have no forecast rows, with a plain-English status.
CREATE OR ALTER VIEW dbo.vw_amazon_order_forecast_asin_status AS
SELECT  r.order_forecast_run_id, r.marketplace, r.method, r.train_end, r.created_at,
        CASE WHEN r.order_forecast_run_id = (SELECT TOP 1 r2.order_forecast_run_id
                                             FROM dbo.amazon_order_forecast_run r2
                                             WHERE r2.marketplace = r.marketplace
                                             ORDER BY r2.train_end DESC, r2.created_at DESC, r2.order_forecast_run_id DESC)
             THEN 1 ELSE 0 END AS is_latest,
        a.asin, m.product_code, m.family, m.product_description,
        CASE WHEN m.product_code IS NULL OR LTRIM(RTRIM(m.product_code)) IN ('', 'NULL')
             THEN 1 ELSE 0 END AS is_unmapped,
        a.status, a.reason_code, a.confidence, a.basis, a.habit_source, a.months_of_history,
        a.has_amazon_forecast, a.has_stock_data, a.last12_units, a.forecast_units,
        CASE a.status
             WHEN 'FORECAST'             THEN 'Forecast produced'
             WHEN 'SHORT_HISTORY'        THEN 'Not forecast: too little sales history'
             WHEN 'NO_SALES'             THEN 'Not forecast: no sales'
             WHEN 'AMAZON_FORECAST_ONLY' THEN 'Not forecast: in Amazon''s forecast but never sold'
             WHEN 'INVENTORY_ONLY'       THEN 'Not forecast: has stock but never sold'
             ELSE a.status END AS status_text
FROM    dbo.amazon_order_forecast_asin_status a
JOIN    dbo.amazon_order_forecast_run r ON r.order_forecast_run_id = a.order_forecast_run_id
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = a.asin AND m.market = r.marketplace;
GO

-- ============================================================================
-- Check after applying (expect one row per run; sanity_status is NULL for runs made before this change):
--   SELECT order_forecast_run_id, marketplace, review_flag, sanity_ratio, sanity_warning
--   FROM dbo.vw_amazon_order_forecast_run_status ORDER BY order_forecast_run_id;
--
-- ROLLBACK (by hand; nothing else depends on these):
--   DROP VIEW dbo.vw_amazon_order_forecast_asin_status;
--   DROP VIEW dbo.vw_amazon_order_forecast_run_status;
--   ALTER TABLE dbo.amazon_order_forecast_run_summary DROP COLUMN sanity_warning;
--   ALTER TABLE dbo.amazon_order_forecast_run_summary DROP COLUMN sanity_status;
-- ============================================================================
