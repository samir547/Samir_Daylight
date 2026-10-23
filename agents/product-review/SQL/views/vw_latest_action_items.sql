-- ============================================================================
-- dbo.vw_latest_action_items -- the most recent generation of business to-do
-- items from dbo.sentiment_action_items (../tables/sentiment_action_items.sql),
-- for the Power BI report read by the Financial Director.
--
-- Returns the 3 rows of the newest generation only (latest generated_at,
-- then the highest item_id as a tie-break). A view can't carry an ORDER BY,
-- so sort by item_rank in the Power BI visual.
--
-- basis is a plain-text line such as
--   "Based on 710 reviews from 2025-08-21 to 2026-08-20"
-- built from the stored window and count, so a reader can see how recent the
-- reviews behind the advice are. The window ends on the newest review date in
-- the data, not on the day the items were generated.
--
-- DEPLOYMENT: applied by an administrator in SSMS. The pipeline's login
-- (daylight_agents) can create the table but not views, so the pipeline never
-- runs this file. Until it is applied, the items are still saved to the table;
-- only this Power BI view is missing.
-- ============================================================================

CREATE OR ALTER VIEW dbo.vw_latest_action_items AS
WITH latest AS (
    SELECT TOP (1) generation_id
    FROM dbo.sentiment_action_items
    ORDER BY generated_at DESC, item_id DESC
)
SELECT
    a.[rank]            AS item_rank,
    a.title,
    a.description,
    a.rationale,
    CASE WHEN a.related_area = 'overall' THEN 'Overall' ELSE a.related_area END
                        AS related_area,
    a.evidence,
    CONCAT('Based on ', a.reviews_analysed, ' reviews from ',
           CONVERT(CHAR(10), a.window_start, 23), ' to ',
           CONVERT(CHAR(10), a.window_end, 23)) AS basis,
    a.window_start,
    a.window_end,
    a.reviews_analysed,
    a.generated_at,
    a.generation_id,
    a.run_timestamp,
    a.llm_provider,
    a.llm_model
FROM dbo.sentiment_action_items AS a
JOIN latest AS l
    ON a.generation_id = l.generation_id;
GO

-- ============================================================================
-- Verification -- run after the view above. Expect exactly 3 rows, ranks
-- 1, 2 and 3, all with the same generation_id.
-- ============================================================================
SELECT item_rank, title, related_area, basis, generated_at
FROM dbo.vw_latest_action_items
ORDER BY item_rank;
