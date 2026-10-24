-- ============================================================================
-- dbo.vw_latest_action_items -- the most recent generation of business to-do
-- items for EACH review window, from dbo.sentiment_action_items
-- (../tables/sentiment_action_items.sql), for the Power BI report read by the
-- Financial Director.
--
-- Each run saves one generation (3 rows) per window_label ("Last 3 months",
-- "Last 6 months", "Last 12 months" by default). This view returns the 3 rows
-- of the newest generation of each window_label (latest generated_at, then
-- the highest item_id as a tie-break). Rows saved before window_label existed
-- (NULL) are treated as one window labelled 'Custom window'.
--
-- A view can't carry an ORDER BY. In Power BI: a single-select slicer on
-- window_label (default "Last 6 months"), and the visual sorted by item_rank.
-- The report's date slicer does not drive these items: they are generated at
-- run time, one fixed set per window.
--
-- basis is a plain-text line such as
--   "Based on 710 reviews from 2025-08-21 to 2026-08-20"
-- built from the stored window and count. When window_end is more than
-- stale_after_days (45, set in the settings CTE below) before today's date
-- at query time, it goes on
--   ". Newest review is from 2026-08-20, so this may be out of date."
-- so a reader can see when no new reviews have arrived for a while.
--
-- evidence_strength reads "Strong", "Moderate" or "Early signal" ("Early
-- signal" when the item rests on too few reviews to be sure).
--
-- DEPLOYMENT: applied by an administrator in SSMS. The pipeline's login
-- (daylight_agents) can create the table but not views, so the pipeline never
-- runs this file. Until it is applied, the items are still saved to the table;
-- only this Power BI view is missing or out of date.
-- ============================================================================

CREATE OR ALTER VIEW dbo.vw_latest_action_items AS
WITH settings AS (
    -- STALE_AFTER_DAYS: a window ending more than this many days before
    -- today gets the out-of-date note in basis.
    SELECT 45 AS stale_after_days
),
labelled AS (
    SELECT a.item_id, a.generation_id, a.generated_at, a.run_timestamp, a.[rank],
           a.title, a.description, a.rationale, a.related_area, a.evidence,
           a.evidence_strength, a.window_months, a.window_start, a.window_end,
           a.reviews_analysed, a.llm_provider, a.llm_model,
           COALESCE(a.window_label, N'Custom window') AS window_group
    FROM dbo.sentiment_action_items AS a
),
ranked AS (
    SELECT generation_id,
           ROW_NUMBER() OVER (PARTITION BY window_group
                              ORDER BY generated_at DESC, item_id DESC) AS newest
    FROM labelled
)
SELECT
    l.window_group      AS window_label,
    l.window_months,
    l.[rank]            AS item_rank,
    l.title,
    l.description,
    l.rationale,
    CASE WHEN l.related_area = 'overall' THEN 'Overall' ELSE l.related_area END
                        AS related_area,
    l.evidence,
    UPPER(LEFT(l.evidence_strength, 1)) + SUBSTRING(l.evidence_strength, 2, 20)
                        AS evidence_strength,
    CONCAT(N'Based on ', l.reviews_analysed, N' reviews from ',
           CONVERT(CHAR(10), l.window_start, 23), N' to ',
           CONVERT(CHAR(10), l.window_end, 23),
           CASE WHEN l.window_end < DATEADD(DAY, -s.stale_after_days,
                                            CAST(SYSDATETIME() AS DATE))
                THEN CONCAT(N'. Newest review is from ',
                            CONVERT(CHAR(10), l.window_end, 23),
                            N', so this may be out of date.')
                ELSE N'' END) AS basis,
    l.window_start,
    l.window_end,
    l.reviews_analysed,
    l.generated_at,
    l.generation_id,
    l.run_timestamp,
    l.llm_provider,
    l.llm_model
FROM labelled AS l
JOIN ranked AS r
    ON r.generation_id = l.generation_id
   AND r.newest = 1
CROSS JOIN settings AS s;
GO

-- ============================================================================
-- Verification -- run after the view above. Expect 3 rows per window_label,
-- ranks 1, 2 and 3, all with the same generation_id within a window_label.
-- ============================================================================
SELECT window_label, item_rank, title, related_area, evidence_strength, basis,
       generated_at
FROM dbo.vw_latest_action_items
ORDER BY window_months, window_label, item_rank;
