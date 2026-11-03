-- ============================================================================
-- 004  Extra column for the "replenish" method: the chance of an order in the month.
--
--   units_forecast = likely order (Amazon mean demand scenario)
--   units_upper    = high case (Amazon P90 demand scenario)
--   order_chance   = share of the three demand scenarios (mean, P80, P90) that place an order that
--                    month: 0, 0.33, 0.67 or 1. An indication, not a calibrated probability.
--
-- Run BY HAND like the other scripts (or via init-db). Re-runnable. The loader checks the column exists,
-- so the forecast still loads (without the chance) if this script could not be applied.
-- ============================================================================
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

IF COL_LENGTH('dbo.amazon_order_forecast', 'order_chance') IS NULL
    ALTER TABLE dbo.amazon_order_forecast ADD order_chance DECIMAL(4,2) NULL;
GO
