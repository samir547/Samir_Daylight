-- ============================================================================
-- 001  Stored order forecasts. NOT APPLIED, NOT YET TESTED: review first.
--
-- Layer 1 of the design (README.md): forecasts are keyed by marketplace + ASIN
-- + month. There is deliberately NO product_code column here. Product codes
-- are added only by the views in 002, so correcting the SKU map never changes
-- or invalidates a stored forecast.
--
-- Run BY HAND, like the amazon-sp-api-ingest scripts. Re-runnable.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.amazon_order_forecast_run', 'U') IS NULL
CREATE TABLE dbo.amazon_order_forecast_run (
    order_forecast_run_id BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_amazon_order_forecast_run PRIMARY KEY,
    marketplace           CHAR(2)       NOT NULL,
    method                VARCHAR(60)   NOT NULL,   -- 'mean12', 'prior_year', 'automl-<job name>', ...
    target_measure        VARCHAR(20)   NOT NULL,   -- 'ordered_units' | 'shipped_units'
    train_end             DATE          NOT NULL,   -- last month of data the forecast used
    horizon_months        TINYINT       NOT NULL,
    snapshot_ref          NVARCHAR(200) NULL,       -- which pulled snapshot it was built from
    created_at            DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_order_forecast_run_ca DEFAULT SYSUTCDATETIME()
);
GO

IF OBJECT_ID('dbo.amazon_order_forecast', 'U') IS NULL
CREATE TABLE dbo.amazon_order_forecast (
    order_forecast_run_id BIGINT        NOT NULL,
    asin                  VARCHAR(10)   NOT NULL,
    month_start           DATE          NOT NULL,
    units_forecast        DECIMAL(12,2) NOT NULL,
    units_lower           DECIMAL(12,2) NULL,
    units_upper           DECIMAL(12,2) NULL,
    CONSTRAINT PK_amazon_order_forecast PRIMARY KEY (order_forecast_run_id, asin, month_start),
    CONSTRAINT FK_amazon_order_forecast_run FOREIGN KEY (order_forecast_run_id)
        REFERENCES dbo.amazon_order_forecast_run (order_forecast_run_id)
);
GO
