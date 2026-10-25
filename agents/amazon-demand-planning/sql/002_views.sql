-- ============================================================================
-- 002  The only place the SKU map touches a forecast. NOT APPLIED, NOT YET TESTED.
--
-- Fixing or extending dbo.amazon_asin_sku_map changes what these views return
-- immediately. It never changes dbo.amazon_order_forecast.
-- ASINs with no usable product code are kept and flagged, never dropped.
--
-- Still to add (needs the invoice-side view and the rule for the six US codes
-- that map to two ASINs): the scoring view that joins forecasts to invoices.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE OR ALTER VIEW dbo.vw_amazon_order_forecast_sku AS
SELECT  r.order_forecast_run_id, r.marketplace, r.method, r.target_measure, r.train_end, r.created_at,
        f.asin, f.month_start, f.units_forecast, f.units_lower, f.units_upper,
        m.product_code, m.family, m.product_description,
        CASE WHEN m.product_code IS NULL OR LTRIM(RTRIM(m.product_code)) IN ('', 'NULL')
             THEN 1 ELSE 0 END AS is_unmapped
FROM    dbo.amazon_order_forecast f
JOIN    dbo.amazon_order_forecast_run r ON r.order_forecast_run_id = f.order_forecast_run_id
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = f.asin AND m.market = r.marketplace;
GO
