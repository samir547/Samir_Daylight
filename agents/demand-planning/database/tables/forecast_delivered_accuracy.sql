-- ============================================================
-- Table: dbo.forecast_delivered_accuracy
-- ------------------------------------------------------------
-- Purpose:
--   Delivered forecast batches scored against the latest
--   actuals. One row per batch, item and month. Rebuilt in
--   full each cycle by evaluation/delivered_accuracy.py, so
--   revised actuals flow through to every batch.
--
-- Deployment:
--   Run this script by hand once, then apply
--   database/views/vw_forecast_delivered_accuracy.sql.
--   Application code never creates this table. If it is
--   missing, scoring warns and the forecast still runs.
--
-- Columns worth a note:
--   training_basis   'development' (trained to anchor-6) or
--                    'production' (trained to the anchor).
--   horizon          Months from the last training month to
--                    year_month: 7-12 or 1-6 respectively.
--   family_key       Successor family, or item_code if none.
--   is_retired_code  1 = sales on a retired code with no
--                    forecast row (yhat 0, model 'retired').
--                    Used for family totals, not item scores.
--   actual           Non-Amazon units; 0 if no sale that month.
--   month_complete   0 = partial or unfinished month; the view
--                    leaves it out until a later cycle.
--   actuals_as_of    When this scoring ran (UTC).
--
-- Re-runnable: creates the table only if it does not exist.
-- ============================================================

IF OBJECT_ID('dbo.forecast_delivered_accuracy', 'U') IS NULL
CREATE TABLE dbo.forecast_delivered_accuracy (
    run_date             DATETIME2     NOT NULL,
    training_basis       NVARCHAR(20)  NOT NULL,
    first_forecast_month NVARCHAR(7)   NOT NULL,
    item_code            NVARCHAR(50)  NOT NULL,
    family_key           NVARCHAR(50)  NOT NULL,
    family               NVARCHAR(200) NULL,
    model                NVARCHAR(20)  NOT NULL,
    year_month           NVARCHAR(7)   NOT NULL,
    horizon              INT           NOT NULL,
    is_retired_code      BIT           NOT NULL,
    yhat                 DECIMAL(12, 2) NOT NULL,
    yhat_lower           DECIMAL(12, 2) NULL,
    yhat_upper           DECIMAL(12, 2) NULL,
    actual               DECIMAL(12, 2) NOT NULL,
    month_complete       BIT           NOT NULL,
    actuals_as_of        DATETIME2     NOT NULL,
    CONSTRAINT PK_forecast_delivered_accuracy
        PRIMARY KEY (run_date, item_code, year_month)
);
