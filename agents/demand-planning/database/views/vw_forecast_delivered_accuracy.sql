-- ============================================================
-- View: dbo.vw_forecast_delivered_accuracy
-- ------------------------------------------------------------
-- Purpose:
--   WAPE and bias of delivered batches, complete months only,
--   one row per batch, month and grain:
--     grain 'item'    each forecast item against its own sales;
--                     retired-code rows are left out.
--     grain 'family'  each successor family's total against all
--                     of its sales, retired codes included, so
--                     errors between sibling codes cancel.
--
-- To roll up to any other level, sum abs_error_units,
-- actual_units and forecast_units first and divide after.
-- Percentages cannot be averaged.
--
-- Re-runnable: CREATE OR ALTER, read-only over the table.
-- ============================================================

CREATE OR ALTER VIEW dbo.vw_forecast_delivered_accuracy AS
WITH scored AS (
    SELECT *
    FROM dbo.forecast_delivered_accuracy
    WHERE month_complete = 1
),
units AS (
    SELECT 'item' AS grain, run_date, training_basis, first_forecast_month,
           year_month, horizon, item_code AS series,
           yhat AS forecast_units, actual AS actual_units
    FROM scored
    WHERE is_retired_code = 0

    UNION ALL

    SELECT 'family' AS grain, run_date, training_basis, first_forecast_month,
           year_month, horizon, family_key AS series,
           SUM(yhat) AS forecast_units, SUM(actual) AS actual_units
    FROM scored
    GROUP BY run_date, training_basis, first_forecast_month,
             year_month, horizon, family_key
)
SELECT
    grain,
    run_date,
    training_basis,
    first_forecast_month,
    year_month,
    horizon,
    COUNT(*)                            AS series_count,
    SUM(forecast_units)                 AS forecast_units,
    SUM(actual_units)                   AS actual_units,
    SUM(ABS(forecast_units - actual_units)) AS abs_error_units,
    CAST(100.0 * SUM(ABS(forecast_units - actual_units))
         / NULLIF(SUM(actual_units), 0) AS DECIMAL(12, 2)) AS wape_pct,
    CAST(100.0 * (SUM(forecast_units) - SUM(actual_units))
         / NULLIF(SUM(actual_units), 0) AS DECIMAL(12, 2)) AS bias_pct
FROM units
GROUP BY grain, run_date, training_basis, first_forecast_month, year_month, horizon;
GO
