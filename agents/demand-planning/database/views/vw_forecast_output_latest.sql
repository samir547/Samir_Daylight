-- ============================================================
-- View: dbo.vw_forecast_output_latest
-- ------------------------------------------------------------
-- Purpose:
--   dbo.forecast_output is append-only — every --write-db run
--   adds a new run_date batch rather than replacing the last
--   one (see loader.py's write_outputs() docstring). This view
--   always resolves to "the current forecast": the most recent
--   run_date, restricted to the models that actually deliver a
--   forecast row (Prophet, Naive-3mo, Naive-12mo). Blocked
--   items have no row in forecast_output at all — that's the
--   routing working as intended, not something this view needs
--   to handle.
--
-- Consumers:
--   - Power BI (Daylight Dashboards 3.7.pbix), table
--     "Forecast Qty (Prophet)", shown alongside the BDM
--     forecast in the Global - Demand Planning page.
--   - Ad-hoc review in SSMS.
--
-- Re-runnable:
--   CREATE OR ALTER — safe to run this script again any time
--   the definition changes. No data is touched; this is a
--   read-only view over forecast_output.
-- ============================================================

CREATE OR ALTER VIEW dbo.vw_forecast_output_latest AS
SELECT
    item_code,
    family,
    year_month,
    yhat,
    yhat_lower,
    yhat_upper,
    model,
    run_kind,
    run_date
FROM dbo.forecast_output
WHERE run_date = (SELECT MAX(run_date) FROM dbo.forecast_output)
  AND model IN ('Prophet', 'Naive-3mo', 'Naive-12mo');
GO
