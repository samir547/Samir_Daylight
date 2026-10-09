-- ============================================================
-- FORECAST TRAINING DATA � FINAL VERSION
-- Grain: Item � BDM � Region � Month
-- Supports both Item-level and Item�BDM-level forecasting
-- BDM attribution: CUSTOMERS table (95% coverage)
-- Region: BDM table (authoritative) with prefix fallback
-- Date parsing: RIGHT(10) + COALESCE (matching Power BI)
-- ============================================================

DROP TABLE IF EXISTS [dbo].[forecast_training_data]

SELECT 
  -- Item identification
  TRIM(i.Item_Code) AS item_code,

  -- BDM attribution (via CUSTOMERS table)
  COALESCE(c.BDM, 'Unattributed') AS bdm_name,
  COALESCE(t.BDM, 'N/A') AS bdm_code,

  -- Region: BDM table first (handles cross-region items), prefix fallback
  COALESCE(
    b_region.Region,
    CASE 
      WHEN LEFT(TRIM(i.Item_Code), 4) = 'DEAN' THEN NULL
      WHEN LEFT(TRIM(i.Item_Code), 2) = 'DN' THEN '1 UK'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'D' THEN '1 UK'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'E' THEN '2 EU'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'U' THEN '3 USA'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'A' THEN '4 AU'
    END
  ) AS region,

  -- Territory context (from CUSTOMERS)
  c.TERRITORY AS territory,
  c.CHANNEL AS channel,

  -- Time dimension
  FORMAT(
    COALESCE(
      TRY_CONVERT(DATE, RIGHT(i.Date, 10), 103),
      TRY_CAST(RIGHT(i.Date, 10) AS DATE)
    ), 'yyyy-MM') AS year_month,

  -- Measures
  SUM(TRY_CAST(REPLACE(i.Qty, ',', '') AS DECIMAL(10,2))) AS monthly_qty,
  COUNT(*) AS transaction_count

INTO [dbo].[forecast_training_data]

FROM [dbo].[INVOICES_TEMP] i

-- BDM from CUSTOMERS table (95% coverage)
LEFT JOIN [dbo].[CUSTOMERS] c 
  ON i.Customer_No = c.ID

-- BDM code from Territory table (CUSTOMERS.BDM = Territory.Department)
LEFT JOIN [dbo].[Territory] t 
  ON c.BDM = t.Department

-- Region from BDM forecast table (authoritative, handles cross-region items).
-- IMPORTANT: this subquery MUST return at most ONE row per (Item_No, BDM).
-- The join predicate below is (Item_No, BDM) only — Region is not in it — so if
-- a single (Item_No, BDM) pair carried more than one Region here, every matching
-- invoice row would fan out to one output row per region, each carrying the full
-- summed Qty. Downstream prepare.py then groups by (item_code, bdm_name) and
-- drops region, summing those duplicate rows → the series is inflated N×.
-- In dbo.BDM only BDM 'SW' (Sarah Whyld) has multi-region (Item_No, BDM) pairs
-- (regions '1 UK' and '2 EU', byte-identical quantities) on a handful of SKUs,
-- so pre-fix her 5 materialised SKUs were doubled exactly 2×. Collapsing to one
-- row per (Item_No, BDM) with GROUP BY eliminates the fan-out at its source.
--
-- Tie-break: MIN(Region) is used purely for determinism. For 'SW' it resolves to
-- '1 UK', which is consistent with her Territory row (Territory 'UK& IE',
-- Company 'UK&EU' — UK primary). NOTE: this is a RESOLVED AMBIGUITY, not a
-- verified-correct region. Because downstream modelling drops region entirely,
-- the choice does not affect forecasts — but it does populate the stored `region`
-- column, so business input may be needed to confirm the true region for these
-- SKUs before that column is used for region-level reporting/rollups. Every
-- collapsed pair is logged to output/region_ambiguity_log.csv by the build script.
LEFT JOIN (
  SELECT Item_No, BDM, MIN(Region) AS Region
  FROM [dbo].[BDM]
  WHERE Region IS NOT NULL
  GROUP BY Item_No, BDM
) b_region
  ON TRIM(i.Item_Code) = b_region.Item_No
  AND t.BDM = b_region.BDM

WHERE i.Item_Code NOT IN ('DISCOUNT', 'Shipping Charge', 'SHIP')
  AND TRY_CAST(REPLACE(i.Qty, ',', '') AS DECIMAL(10,2)) > 0
  AND (LEFT(TRIM(i.Item_Code), 2) IN ('DN')
    OR LEFT(TRIM(i.Item_Code), 1) IN ('A', 'D', 'E', 'U'))

GROUP BY 
  TRIM(i.Item_Code),
  COALESCE(c.BDM, 'Unattributed'),
  COALESCE(t.BDM, 'N/A'),
  COALESCE(
    b_region.Region,
    CASE 
      WHEN LEFT(TRIM(i.Item_Code), 4) = 'DEAN' THEN NULL
      WHEN LEFT(TRIM(i.Item_Code), 2) = 'DN' THEN '1 UK'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'D' THEN '1 UK'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'E' THEN '2 EU'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'U' THEN '3 USA'
      WHEN LEFT(TRIM(i.Item_Code), 1) = 'A' THEN '4 AU'
    END
  ),
  c.TERRITORY,
  c.CHANNEL,
  FORMAT(
    COALESCE(
      TRY_CONVERT(DATE, RIGHT(i.Date, 10), 103),
      TRY_CAST(RIGHT(i.Date, 10) AS DATE)
    ), 'yyyy-MM')