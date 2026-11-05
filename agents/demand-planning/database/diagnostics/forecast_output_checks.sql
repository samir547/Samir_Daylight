-- ============================================================
-- Diagnostics: dbo.forecast_output — pre-integration checks
-- Run these in SSMS before wiring a new batch into Power BI.
-- All read-only. Used 2026-09-05/06 to validate the batch
-- that vw_forecast_output_latest now surfaces.
-- ============================================================

-- 1. What models/run_kinds/batches exist, and item coverage per batch.
--    A clean run has rows/items dividing evenly by month count
--    (e.g. 153 items x 7 months = 1,071 rows) — an uneven ratio
--    means some items have partial month coverage, worth
--    investigating before trusting the batch.
SELECT run_date, model, run_kind, COUNT(*) AS rows, COUNT(DISTINCT item_code) AS items,
       MIN(year_month) AS min_month, MAX(year_month) AS max_month
FROM dbo.forecast_output
GROUP BY run_date, model, run_kind
ORDER BY run_date DESC, model;

-- 2. Delivered accuracy, newest batches first. Production runs do
--    not write dbo.forecast_accuracy, so that table only gains rows
--    from development runs; a batch missing from it is expected.
--    Complete months only. A new batch appears once its first month
--    has actuals. Needs the table and view in database/.
SELECT grain, first_forecast_month, training_basis, year_month, horizon,
       series_count, forecast_units, actual_units, wape_pct, bias_pct
FROM dbo.vw_forecast_delivered_accuracy
ORDER BY first_forecast_month DESC, year_month, grain;

-- 3. item_code whitespace check — untrimmed values silently break
--    the join to Map_Master Product Table[NAME] in Power BI.
SELECT item_code FROM dbo.forecast_output WHERE item_code <> LTRIM(RTRIM(item_code));

-- 4. Orphan check against the master product table — same TRIM
--    pattern the pipeline's own MASTER_PRODUCT query already uses.
SELECT DISTINCT fo.item_code
FROM dbo.forecast_output fo
LEFT JOIN [dbo].[MASTER RODUCT TABLE] mpt ON TRIM(mpt.[NAME]) = TRIM(fo.item_code)
WHERE mpt.[NAME] IS NULL;

-- 5. forecast_training_data freshness spot-check — confirms the
--    staging table (rebuilt by this pipeline from INVOICES_TEMP,
--    which is refreshed externally) actually has data for a given
--    month before trusting an anchor on it. Replace '2026-08' as
--    needed.
SELECT MAX(year_month), COUNT(*) FROM dbo.forecast_training_data WHERE year_month = '2026-08';
