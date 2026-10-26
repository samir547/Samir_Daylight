-- ============================================================================
-- 003  OPTIONAL. Adds the foreign key from forecast rows to their run.
-- Needs an admin login (REFERENCES permission). The tool works without it.
-- Run BY HAND. Re-runnable.
-- ============================================================================
IF NOT EXISTS (SELECT 1 FROM sys.foreign_keys WHERE name = 'FK_amazon_order_forecast_run')
    ALTER TABLE dbo.amazon_order_forecast
        ADD CONSTRAINT FK_amazon_order_forecast_run FOREIGN KEY (order_forecast_run_id)
        REFERENCES dbo.amazon_order_forecast_run (order_forecast_run_id);
GO
