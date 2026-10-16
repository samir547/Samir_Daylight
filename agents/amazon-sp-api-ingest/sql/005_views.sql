-- ============================================================================
-- 005  Views  (data-ingestion-spec.md sections 7.1 and 7.3, decisions D7 to D9)
--
-- Every fact view returns ONE grain only (WEEK or MONTH), excludes the 'ALL'
-- rows, and LEFT JOINs the SKU map on both asin and market (D7), so
-- unmapped ASINs are kept with a NULL product_code. Each exposes
-- period_type, is_partial and loaded_through:
--   loaded_through  latest period_end loaded for that marketplace and grain
--   is_partial      weekly views: 1 when the week's month (the month of its
--                   Saturday, D8) is the month of loaded_through and another
--                   Saturday is still due in it; monthly views: always 0
--
-- Requires 002, 003, 004 and the existing dbo.amazon_asin_sku_map.
-- Before relying on the _sku views, confirm (asin, market) is unique in the
-- SKU map (007_checks.sql, check 1); otherwise these views multiply rows.
-- Run BY HAND (section 7.2). Re-runnable: CREATE OR ALTER VIEW.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

CREATE OR ALTER VIEW dbo.vw_amazon_vendor_sales_weekly_sku AS
SELECT s.marketplace, s.asin, m.product_code, m.family, m.product_description,
       s.period_type, s.period_start, s.period_end,
       DATEFROMPARTS(YEAR(s.period_end), MONTH(s.period_end), 1) AS belongs_to_month,  -- D8: month of the Saturday
       CAST(CASE WHEN EOMONTH(s.period_end) = EOMONTH(lt.loaded_through)
                  AND DATEADD(day, 7, lt.loaded_through) <= EOMONTH(lt.loaded_through)  -- another Saturday still due
                 THEN 1 ELSE 0 END AS BIT) AS is_partial,
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
       CAST(0 AS BIT) AS is_partial,          -- MONTH rows exist only for closed months
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

-- Inventory is a snapshot (taken on the Saturday for WEEK rows). Consumers
-- must not SUM inventory across periods.
CREATE OR ALTER VIEW dbo.vw_amazon_vendor_inventory_weekly_sku AS
SELECT i.marketplace, i.asin, m.product_code, m.family, m.product_description,
       i.period_type, i.period_start, i.period_end,
       DATEFROMPARTS(YEAR(i.period_end), MONTH(i.period_end), 1) AS belongs_to_month,  -- D8: month of the Saturday
       CAST(CASE WHEN EOMONTH(i.period_end) = EOMONTH(lt.loaded_through)
                  AND DATEADD(day, 7, lt.loaded_through) <= EOMONTH(lt.loaded_through)
                 THEN 1 ELSE 0 END AS BIT) AS is_partial,
       lt.loaded_through,
       i.net_received_units, i.net_received_cost, i.open_po_units,
       i.sellable_units, i.sellable_cost, i.unsellable_units, i.unsellable_cost,
       i.sell_through_rate, i.aged_90plus_units, i.aged_90plus_cost,
       i.unhealthy_units, i.unhealthy_cost, i.currency
FROM dbo.amazon_vendor_inventory i
JOIN (SELECT marketplace, MAX(period_end) AS loaded_through
      FROM dbo.amazon_vendor_inventory WHERE period_type = 'WEEK'
      GROUP BY marketplace) lt ON lt.marketplace = i.marketplace
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = i.asin AND m.market = i.marketplace
WHERE i.period_type = 'WEEK' AND i.asin <> 'ALL';
GO

CREATE OR ALTER VIEW dbo.vw_amazon_vendor_inventory_monthly_sku AS
SELECT i.marketplace, i.asin, m.product_code, m.family, m.product_description,
       i.period_type, i.period_start, i.period_end,
       CAST(0 AS BIT) AS is_partial,
       lt.loaded_through,
       i.net_received_units, i.net_received_cost, i.open_po_units,
       i.sellable_units, i.sellable_cost, i.unsellable_units, i.unsellable_cost,
       i.sell_through_rate, i.aged_90plus_units, i.aged_90plus_cost,
       i.unhealthy_units, i.unhealthy_cost, i.currency
FROM dbo.amazon_vendor_inventory i
JOIN (SELECT marketplace, MAX(period_end) AS loaded_through
      FROM dbo.amazon_vendor_inventory WHERE period_type = 'MONTH'
      GROUP BY marketplace) lt ON lt.marketplace = i.marketplace
LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = i.asin AND m.market = i.marketplace
WHERE i.period_type = 'MONTH' AND i.asin <> 'ALL';
GO

-- All forecast versions, numbered per (marketplace, selling_program) in
-- generation-date order, with the latest flagged.
CREATE OR ALTER VIEW dbo.vw_amazon_forecast_versioned AS
SELECT r.marketplace, r.selling_program, r.forecast_generation_date,
       DENSE_RANK() OVER (PARTITION BY r.marketplace, r.selling_program
                          ORDER BY r.forecast_generation_date) AS version_no,
       CAST(CASE WHEN r.forecast_generation_date =
                      MAX(r.forecast_generation_date) OVER (PARTITION BY r.marketplace, r.selling_program)
                 THEN 1 ELSE 0 END AS BIT) AS is_latest,
       w.asin, w.week_start, w.week_end, w.weeks_ahead,
       w.mean_units, w.p70_units, w.p80_units, w.p90_units
FROM dbo.amazon_forecast_run r
JOIN dbo.amazon_forecast_weekly w ON w.forecast_run_id = r.forecast_run_id;
GO
