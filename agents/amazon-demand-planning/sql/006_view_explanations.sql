-- ============================================================================
-- 006  The Power BI view, with the explanation columns from 005.
--
-- Needs 005 applied first. Run BY HAND. Re-runnable (CREATE OR ALTER).
--
-- Identical to sql/002_views.sql except for four added columns:
-- f.basis, f.reason_code, f.reason_detail, f.confidence. The SKU-map join and
-- is_latest are copied unchanged (tests/test_store_schema.py checks that).
--
-- Note: once this is applied, `init-db` skips 002 (it checks whether the view
-- already has reason_code), so it no longer narrows the view back.
-- Note: is_latest still picks the newest run per marketplace whatever the
-- method. When adjustments exist the loader stores replenish_unadjusted FIRST
-- and the main replenish run last, so the main run holds the flag. A run of
-- another method loaded later by hand would take it: then also filter on
-- method = 'replenish'.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE OR ALTER VIEW dbo.vw_amazon_order_forecast_sku AS
SELECT  r.order_forecast_run_id, r.marketplace, r.method, r.target_measure, r.train_end, r.created_at,
        f.asin, f.month_start, f.units_forecast, f.units_lower, f.units_upper, f.order_chance,
        m.product_code, m.family, m.product_description,
        CASE WHEN m.product_code IS NULL OR LTRIM(RTRIM(m.product_code)) IN ('', 'NULL')
             THEN 1 ELSE 0 END AS is_unmapped,
        CASE WHEN r.order_forecast_run_id = (SELECT TOP 1 r2.order_forecast_run_id
                                             FROM dbo.amazon_order_forecast_run r2
                                             WHERE r2.marketplace = r.marketplace
                                             ORDER BY r2.train_end DESC, r2.created_at DESC, r2.order_forecast_run_id DESC)
             THEN 1 ELSE 0 END AS is_latest,       -- Power BI: filter is_latest = 1 for the current forecast
        f.basis, f.reason_code, f.reason_detail, f.confidence
FROM    dbo.amazon_order_forecast f
JOIN    dbo.amazon_order_forecast_run r ON r.order_forecast_run_id = f.order_forecast_run_id
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = f.asin AND m.market = r.marketplace;
GO

-- ============================================================================
-- ROLLBACK (by hand): re-apply sql/002_views.sql. That restores the previous
-- column list; nothing else changes. Do this before rolling back 005.
-- ============================================================================
