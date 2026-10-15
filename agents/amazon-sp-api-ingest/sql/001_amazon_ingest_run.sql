-- ============================================================================
-- 001  dbo.amazon_ingest_run  (data-ingestion-spec.md section 7.3)
--
-- Audit log: one row per job run per marketplace. Every fact row points at
-- the last run that inserted or changed it (last_run_id), so this script
-- must run before 002, 003 and 004.
--
-- Run BY HAND by someone with database rights (section 7.2), never from
-- application code. Re-runnable: creates the table only if it is missing,
-- and never alters or drops an existing one.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF OBJECT_ID('dbo.amazon_ingest_run', 'U') IS NULL
CREATE TABLE dbo.amazon_ingest_run (
    run_id        BIGINT IDENTITY(1,1) NOT NULL CONSTRAINT PK_amazon_ingest_run PRIMARY KEY,
    job_name      VARCHAR(30)    NOT NULL,   -- 'sales' | 'inventory' | 'forecast'
    marketplace   CHAR(2)        NOT NULL,
    trigger_type  VARCHAR(20)    NOT NULL,   -- 'scheduled' | 'manual' | 'backfill' | 'import'
    window_start  DATE           NULL,       -- sales/inventory: first period requested
    window_end    DATE           NULL,       -- sales/inventory: last period requested
    status        VARCHAR(20)    NOT NULL,   -- 'running' | 'success' | 'partial' | 'failed' | 'no_data'
    rows_read     INT            NULL,
    rows_inserted INT            NULL,
    rows_updated  INT            NULL,       -- existing rows whose values changed (restatements)
    started_at    DATETIME2(0)   NOT NULL CONSTRAINT DF_amazon_ingest_run_start DEFAULT SYSUTCDATETIME(),
    finished_at   DATETIME2(0)   NULL,
    message       NVARCHAR(2000) NULL,       -- includes weeks "skipped, outside lookback" (section 9.1)
    host_name     NVARCHAR(128)  NULL,
    -- no_data = nothing new for this marketplace (D10); it is not a failure.
    CONSTRAINT CK_amazon_ingest_run_status
        CHECK (status IN ('running', 'success', 'partial', 'failed', 'no_data'))
);
GO
