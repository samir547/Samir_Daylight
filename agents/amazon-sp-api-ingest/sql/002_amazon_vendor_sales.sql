-- ============================================================================
-- 002  dbo.amazon_vendor_sales  (data-ingestion-spec.md section 7.3)
--
-- Sales fact table, WEEK and MONTH rows side by side (D8), including the
-- account-level 'ALL' rows (validation only, never demand planning).
-- NEVER SUM ACROSS period_type: the same dates exist at both grains (D9).
-- Read through the views in 005 instead.
--
-- Requires 001 (foreign key to dbo.amazon_ingest_run).
-- Run BY HAND (section 7.2). Re-runnable: table and index are created only
-- if missing.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.amazon_vendor_sales', 'U') IS NULL
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
    CONSTRAINT CK_amazon_vendor_sales_pt CHECK (period_type IN ('WEEK', 'MONTH')),
    CONSTRAINT FK_amazon_vendor_sales_run FOREIGN KEY (last_run_id) REFERENCES dbo.amazon_ingest_run (run_id)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_amazon_vendor_sales_asin' AND object_id = OBJECT_ID('dbo.amazon_vendor_sales'))
    CREATE INDEX IX_amazon_vendor_sales_asin ON dbo.amazon_vendor_sales (asin, marketplace, period_start);
GO
