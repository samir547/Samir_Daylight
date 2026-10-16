-- ============================================================================
-- 004  dbo.amazon_forecast_run, dbo.amazon_forecast_weekly
--      (data-ingestion-spec.md sections 7.3 and 8, decisions D5 and D12)
--
-- One header row per forecast version, identified by
-- (marketplace, selling_program, forecast_generation_date), plus about
-- 48 weekly rows per ASIN. Append-only, except that a version pulled again
-- with different content replaces its own weekly rows (D5).
--
-- Requires 001 (foreign key to dbo.amazon_ingest_run).
-- Run BY HAND (section 7.2). Re-runnable: tables and index are created only
-- if missing.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.amazon_forecast_run', 'U') IS NULL
CREATE TABLE dbo.amazon_forecast_run (
    forecast_run_id          BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_amazon_forecast_run PRIMARY KEY,
    marketplace              CHAR(2)       NOT NULL,
    selling_program          VARCHAR(10)   NOT NULL CONSTRAINT DF_amazon_forecast_run_sp DEFAULT 'RETAIL',
    forecast_generation_date DATE          NOT NULL,   -- Amazon's date; the version identity
    report_last_updated      DATE          NULL,       -- reportSpecification.lastUpdatedDate
    pulled_at                DATETIME2(0)  NOT NULL CONSTRAINT DF_amazon_forecast_run_pa DEFAULT SYSUTCDATETIME(),
    asin_count               INT           NOT NULL,
    row_count                INT           NOT NULL,
    week_count               SMALLINT      NOT NULL,   -- expected 48
    ingest_run_id            BIGINT        NOT NULL,
    raw_file_path            NVARCHAR(500) NULL,       -- archived raw JSON on the VM (section 8.5)
    CONSTRAINT UQ_amazon_forecast_run UNIQUE (marketplace, selling_program, forecast_generation_date),
    CONSTRAINT FK_amazon_forecast_run_ingest FOREIGN KEY (ingest_run_id) REFERENCES dbo.amazon_ingest_run (run_id)
);
GO

IF OBJECT_ID('dbo.amazon_forecast_weekly', 'U') IS NULL
CREATE TABLE dbo.amazon_forecast_weekly (
    forecast_run_id BIGINT        NOT NULL,
    asin            VARCHAR(10)   NOT NULL,
    week_start      DATE          NOT NULL,   -- Sunday
    week_end        DATE          NOT NULL,   -- Saturday
    weeks_ahead     SMALLINT      NOT NULL,   -- 0..47
    mean_units      DECIMAL(12,2) NULL,
    p70_units       DECIMAL(12,2) NULL,
    p80_units       DECIMAL(12,2) NULL,
    p90_units       DECIMAL(12,2) NULL,
    CONSTRAINT PK_amazon_forecast_weekly PRIMARY KEY (forecast_run_id, asin, week_start),
    CONSTRAINT FK_amazon_forecast_weekly_run FOREIGN KEY (forecast_run_id) REFERENCES dbo.amazon_forecast_run (forecast_run_id)
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_amazon_forecast_weekly_asin' AND object_id = OBJECT_ID('dbo.amazon_forecast_weekly'))
    CREATE INDEX IX_amazon_forecast_weekly_asin ON dbo.amazon_forecast_weekly (asin, week_start);
GO
