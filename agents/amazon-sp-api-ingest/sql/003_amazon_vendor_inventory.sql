-- ============================================================================
-- 003  dbo.amazon_vendor_inventory  (data-ingestion-spec.md section 7.3)
--
-- Inventory fact table, WEEK and MONTH rows side by side (D8), including the
-- 'ALL' rows (validation only). Inventory is a POINT-IN-TIME SNAPSHOT (the
-- weekly snapshot is taken on the Saturday, probe T0): never sum it across
-- weeks or months, and never across period_type (D9).
-- NULL means "not supplied by Amazon", not zero.
--
-- Requires 001 (foreign key to dbo.amazon_ingest_run).
-- Run BY HAND (section 7.2). Re-runnable: table and index are created only
-- if missing.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.amazon_vendor_inventory', 'U') IS NULL
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
    CONSTRAINT CK_amazon_vendor_inv_pt CHECK (period_type IN ('WEEK', 'MONTH')),
    CONSTRAINT FK_amazon_vendor_inv_run FOREIGN KEY (last_run_id) REFERENCES dbo.amazon_ingest_run (run_id)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_amazon_vendor_inventory_asin' AND object_id = OBJECT_ID('dbo.amazon_vendor_inventory'))
    CREATE INDEX IX_amazon_vendor_inventory_asin ON dbo.amazon_vendor_inventory (asin, marketplace, period_start);
GO
