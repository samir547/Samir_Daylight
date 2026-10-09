"""
All SQL used by the pipeline, as named string constants.

The heavy lifting (cleaning, attribution, monthly aggregation) is already done
in [dbo].[forecast_training_data], so we read from that staging table directly
instead of rebuilding from raw INVOICES_TEMP on every run.
"""

# Clean, validated, pre-aggregated training data.
# Grain: Item × BDM × Region × Month (Jan 2018 onward, no upper bound — the
# newest month may be partial; analysis/month_completeness.py decides which
# month is safe to anchor on).
TRAINING_DATA = """
SELECT *
FROM [dbo].[forecast_training_data]
"""

# BDM manual forecasts, unpivoted from wide (Jan..Dec columns) to long (one row
# per month). Monthly figures are stored as NVARCHAR and coerced to DECIMAL.
#
# No year filter. [dbo].[BDM] is one row per (Item_No, BDM, Year) and currently
# holds both 2026 and 2027 rows for the same items (2027 planning has started).
# This query returns EVERY year present — the old :bdm_year filter and the
# single-year guardrail it existed for are gone, because
# evaluation.benchmark._bdm_month_col() now builds its join key from each row's
# own `forecast_year` column instead of a global constant, so multi-year rows
# can no longer collide under one month label. loader.load_bdm_forecasts() no
# longer binds a year parameter for this query.
BDM_FORECASTS = """
SELECT
    Item_No                         AS item_code,
    BDM                             AS bdm_code,
    Region                          AS region,
    Rating                          AS rating,
    [Year]                          AS forecast_year,
    month_name,
    TRY_CAST(qty AS DECIMAL(10, 2)) AS bdm_forecast_qty
FROM [dbo].[BDM]
UNPIVOT (
    qty FOR month_name IN
        (Jan, Feb, Mar, Apr, May, Jun, Jul, Aug, Sep, Oct, Nov, Dec)
) AS unpvt
WHERE Region IS NOT NULL
"""

# Active products: items present on the BDM sheet that also still have recent
# trading activity in the training data. The cut-off is config.ACTIVE_SINCE,
# bound as :active_since (loader.load_active_products) — it is NOT rolled
# automatically, bump it by hand in config.py.
ACTIVE_PRODUCTS = """
SELECT DISTINCT Item_No AS item_code
FROM [dbo].[BDM]
WHERE Region IS NOT NULL
  AND Item_No IN (
    SELECT DISTINCT item_code
    FROM [dbo].[forecast_training_data]
    WHERE year_month >= :active_since
  )
"""

# Product Family reporting tag, keyed on item_code.
# Source: [MASTER RODUCT TABLE] (sic — the table name has a typo in the DB).
# [NAME] is 99.9% unique; the lone duplicate (SN1200B) is outside the pipeline's
# item-code prefix gate, so a straight read cannot fan out the item-level join.
# DISTINCT guards against that edge case regardless.
MASTER_PRODUCT = """
SELECT DISTINCT
    TRIM([NAME])            AS item_code,
    [PRODUCT FAMILY]        AS family
FROM [dbo].[MASTER RODUCT TABLE]
WHERE [NAME] IS NOT NULL
"""

# Old → new product-code successor relation, with the review context needed to
# decide whether a family may be pooled and when its changeover happened.
# [product_successor_map] is the clean, derived relation (one row per old→new
# pairing); [product_successor_review] is the audit trail it was derived from.
# Both are maintained outside this pipeline — read-only here.
SUCCESSOR_MAP = """
SELECT
    m.map_id,
    m.review_id,
    m.old_item_code,
    m.new_item_code,
    m.source,
    m.confidence,
    r.status,
    r.estimated_changeover
FROM [dbo].[product_successor_map] m
LEFT JOIN [dbo].[product_successor_review] r
       ON r.review_id = m.review_id
"""

# ── Optional write-back targets ───────────────────────────────────────────────

# Grain change (2026-07): forecasts are now item-level, so bdm_name is dropped
# from both tables and `family` (Product Family reporting tag) is added.
# NOTE: these are IF-NOT-EXISTS creates — an already-existing table at the old
# (bdm_name) grain is NOT altered here. Drop dbo.forecast_output /
# dbo.forecast_accuracy once before the first item-grain --write-db run so the
# new schema takes effect.
CREATE_FORECAST_OUTPUT = """
IF OBJECT_ID('dbo.forecast_output', 'U') IS NULL
CREATE TABLE dbo.forecast_output (
    item_code   NVARCHAR(50),
    family      NVARCHAR(200),
    year_month  NVARCHAR(7),
    yhat        DECIMAL(12, 2),
    yhat_lower  DECIMAL(12, 2),
    yhat_upper  DECIMAL(12, 2),
    model       NVARCHAR(20),
    run_kind    NVARCHAR(20),
    run_date    DATETIME2
)
"""

CREATE_FORECAST_ACCURACY = """
IF OBJECT_ID('dbo.forecast_accuracy', 'U') IS NULL
CREATE TABLE dbo.forecast_accuracy (
    item_code   NVARCHAR(50),
    family      NVARCHAR(200),
    model       NVARCHAR(20),
    mae         DECIMAL(12, 2),
    rmse        DECIMAL(12, 2),
    mape_pct    DECIMAL(12, 2),
    n_train_months INT,
    n_test_months  INT,
    run_date    DATETIME2
)
"""
