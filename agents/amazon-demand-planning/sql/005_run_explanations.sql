-- ============================================================================
-- 005  Why each forecast number is what it is, per row, per ASIN and per run.
--
-- Run BY HAND (admin or the shared login, if it may ALTER/CREATE). Re-runnable:
-- every statement checks first. NOT applied by `init-db` and never by the
-- loader: application code does not run DDL.
--
-- The loader (order_forecast/store.py) checks INFORMATION_SCHEMA before each
-- load and writes only what exists, so the monthly job keeps working before,
-- during and after this script (it logs a WARNING while something is missing).
--
-- 1. Four columns on dbo.amazon_order_forecast, all NULL with no default, so
--    the older methods (seasonal, mean12, ...) keep inserting only their
--    legacy columns:
--      basis          'replay' | 'sales_only'                 (replenish only)
--      reason_code    the rule that decided the likely case     e.g. REORDER_TRIGGERED
--      reason_detail  every rule that touched the row, '|'-separated codes,
--                     primary first (codes only, never free text)
--      confidence     'HIGH' | 'MEDIUM' | 'LOW'
--    The codes are listed in order_forecast/replenish.py (REASON_CODES) and
--    docs/replenish.md.
-- 2. dbo.amazon_order_forecast_asin_status: one row per run and ASIN seen in
--    sales, Amazon's forecast or inventory, forecast or not, and why.
-- 3. dbo.amazon_order_forecast_run_summary: one row per run with the counts.
--
-- No foreign keys, for the same reason as 001 (the shared login has no
-- REFERENCES permission). The loader writes all tables of a run in one
-- transaction and deletes them together when a run is replaced.
-- Rollback: see the block at the bottom.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH('dbo.amazon_order_forecast', 'basis') IS NULL
    ALTER TABLE dbo.amazon_order_forecast ADD basis VARCHAR(20) NULL;
GO
IF COL_LENGTH('dbo.amazon_order_forecast', 'reason_code') IS NULL
    ALTER TABLE dbo.amazon_order_forecast ADD reason_code VARCHAR(40) NULL;
GO
IF COL_LENGTH('dbo.amazon_order_forecast', 'reason_detail') IS NULL
    ALTER TABLE dbo.amazon_order_forecast ADD reason_detail VARCHAR(500) NULL;
GO
IF COL_LENGTH('dbo.amazon_order_forecast', 'confidence') IS NULL
    ALTER TABLE dbo.amazon_order_forecast ADD confidence VARCHAR(6) NULL;
GO

IF OBJECT_ID('dbo.amazon_order_forecast_asin_status', 'U') IS NULL
CREATE TABLE dbo.amazon_order_forecast_asin_status (
    order_forecast_run_id BIGINT        NOT NULL,
    asin                  VARCHAR(10)   NOT NULL,
    status                VARCHAR(30)   NOT NULL,   -- FORECAST | SHORT_HISTORY | NO_SALES | AMAZON_FORECAST_ONLY | INVENTORY_ONLY
    reason_code           VARCHAR(40)   NOT NULL,   -- REPLAY | SALES_ONLY_NO_STOCK | YOUNG_PRODUCT_BACKUP, else = status
    confidence            VARCHAR(6)    NULL,       -- lowest row confidence of the ASIN; NULL when not forecast
    basis                 VARCHAR(20)   NULL,       -- 'replay' | 'sales_only'; NULL when not forecast
    habit_source          VARCHAR(10)   NULL,       -- 'own' | 'blended' | 'pooled'; NULL when not forecast
    months_of_history     SMALLINT      NOT NULL,   -- months since the first sales row (0 = not in sales)
    has_amazon_forecast   BIT           NOT NULL,   -- Amazon's latest forecast covers at least one forecast month
    has_stock_data        BIT           NOT NULL,   -- a stock reading in the last 3 months of data
    last12_units          DECIMAL(14,2) NULL,       -- sales in the last 12 months
    forecast_units        DECIMAL(14,2) NULL,       -- likely-case orders over the horizon; NULL when not forecast
    CONSTRAINT PK_amazon_order_forecast_asin_status PRIMARY KEY (order_forecast_run_id, asin)
);
GO

IF OBJECT_ID('dbo.amazon_order_forecast_run_summary', 'U') IS NULL
CREATE TABLE dbo.amazon_order_forecast_run_summary (
    order_forecast_run_id          BIGINT        NOT NULL CONSTRAINT PK_amazon_order_forecast_run_summary PRIMARY KEY,
    asins_in_sales                 INT           NOT NULL,
    asins_forecast                 INT           NOT NULL,   -- standard + young-product backups
    young_product_backups          INT           NOT NULL,
    skipped_short_history          INT           NOT NULL,   -- under young_min_months of sales
    skipped_no_sales               INT           NOT NULL,   -- under min_history and no sales at all
    asins_without_amazon_forecast  INT           NOT NULL,   -- forecast ASINs that Amazon's forecast does not cover
    amazon_only_dropped            INT           NOT NULL,   -- in Amazon's forecast, not in sales
    inventory_only_dropped         INT           NOT NULL,   -- in inventory only
    sales_only_asins               INT           NOT NULL,   -- forecast ASINs with stock unknown
    default_habit_asins            INT           NOT NULL,   -- replayed ASINs on the market's typical habits (no own orders)
    seasonality_mode               VARCHAR(10)   NOT NULL,   -- 'learned' | 'flat' (under 24 months of market history)
    market_months                  SMALLINT      NOT NULL,
    sanity_ratio                   DECIMAL(9,3)  NULL,       -- the checked ratio: forecast vs recent pace of the same ASINs
    sanity_ratio_all_asins         DECIMAL(9,3)  NULL,       -- for information: base includes skipped ASINs
    skipped_sales_share            DECIMAL(7,4)  NULL,       -- skipped ASINs' share of last-12-month sales
    amazon_generation_date         DATE          NULL,       -- Amazon forecast version used; NULL = none
    summary_json                   NVARCHAR(MAX) NULL,       -- the full run_summary.json
    created_at                     DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_order_forecast_run_summary_ca DEFAULT SYSUTCDATETIME()
);
GO

-- ============================================================================
-- ROLLBACK (by hand). Undo sql/006 FIRST (re-apply sql/002_views.sql), because
-- the 006 view reads the columns dropped here. Then:
--
--   DROP TABLE IF EXISTS dbo.amazon_order_forecast_run_summary;
--   DROP TABLE IF EXISTS dbo.amazon_order_forecast_asin_status;
--   ALTER TABLE dbo.amazon_order_forecast DROP COLUMN IF EXISTS confidence;
--   ALTER TABLE dbo.amazon_order_forecast DROP COLUMN IF EXISTS reason_detail;
--   ALTER TABLE dbo.amazon_order_forecast DROP COLUMN IF EXISTS reason_code;
--   ALTER TABLE dbo.amazon_order_forecast DROP COLUMN IF EXISTS basis;
--
-- The loader notices on its next run and goes back to the legacy columns.
-- ============================================================================
