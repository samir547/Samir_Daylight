-- ============================================================================
-- dbo.vw_latest_action_items -- the most recent generation of business to-do
-- items for EACH review window and EACH review scope, from
-- dbo.sentiment_action_items (../tables/sentiment_action_items.sql), for the
-- Power BI report read by the Financial Director.
--
-- Each run saves one generation per window_label ("Last 3 months", "Last 6
-- months", "Last 12 months" by default) and review scope (source_scope /
-- review_type_scope): 3 rows, or ONE placeholder row (is_placeholder = 1)
-- when that scope and window had too few reviews for items. This view
-- returns the rows of the newest generation of each (window_label,
-- source_scope, review_type_scope): latest generated_at, then the highest
-- item_id as a tie-break. A placeholder that is the newest generation
-- replaces the older items of its scope and window: they no longer apply.
-- Rows saved before window_label existed (NULL) are treated as one window
-- labelled 'Custom window'; rows saved before the scope columns existed
-- (NULL) cover every review and are read as All / All.
--
-- SCOPES. The pipeline stores six scopes:
--     All / All           All / Product          Amazon / Product
--     Trustpilot / All    Trustpilot / Product   Trustpilot / Service
-- Two more combinations hold exactly the same reviews as a stored one, so
-- the view shows the stored generation under them as well (the UNION ALL in
-- shown_scope), and both slicers work in every combination without extra
-- model calls:
--     Amazon / All   = Amazon / Product     (Amazon has product reviews only)
--     All / Service  = Trustpilot / Service (service reviews are Trustpilot only)
-- Amazon / Service does not exist: there are no such reviews, so the view
-- has no rows for it and that slicer combination shows nothing. The pipeline
-- never stores Amazon / All or All / Service itself; if it ever did, those
-- rows would appear next to the copies.
--
-- A view can't carry an ORDER BY. In Power BI: single-select slicers on
-- window_label (default "Last 6 months"), source_scope (default "All") and
-- review_type_scope (default "All"), and the visual sorted by item_rank.
-- The report's date slicer does not drive these items: they are generated at
-- run time, one fixed set per window and scope.
--
-- basis is a plain-text line such as
--   "Based on 710 reviews from 2025-08-21 to 2026-08-20"
-- built from the stored window and count. On a placeholder row it reads
--   "Too few reviews for to-do items: 18 from 2026-05-21 to 2026-08-20"
-- or, when the scope has no dated reviews at all (reviews_analysed = 0, and
-- the stored window is not that scope's own),
--   "Too few reviews for to-do items: none with a review date in this selection"
-- When window_end is more than stale_after_days (45, set in the settings CTE
-- below) before today's date at query time, it goes on
--   ". Newest review is from 2026-08-20, so this may be out of date."
-- so a reader can see when no new reviews have arrived for a while. The note
-- is left off a placeholder with no reviews, whose window_end is not the
-- date of one of its own reviews.
--
-- evidence_strength reads "Strong", "Moderate" or "Early signal" ("Early
-- signal" when the item rests on too few reviews to be sure); it is NULL on
-- a placeholder row.
--
-- DEPLOYMENT: applied by an administrator in SSMS. The pipeline's login
-- (daylight_agents) can create the table but not views, so the pipeline never
-- runs this file. Until it is applied, the items are still saved to the table;
-- only this Power BI view is missing or out of date. It needs the columns
-- source_scope, review_type_scope and is_placeholder: run
-- ../tables/sentiment_action_items.sql first if the pipeline has not yet
-- saved anything since they were introduced. Apply it BEFORE the first run
-- that saves scoped items: the previous version of this view picks one
-- generation per window_label whatever its scope.
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
           a.reviews_analysed, a.llm_provider, a.llm_model, a.is_placeholder,
           COALESCE(a.window_label, N'Custom window') AS window_group,
           COALESCE(a.source_scope, N'All') AS stored_source_scope,
           COALESCE(a.review_type_scope, N'All') AS stored_review_type_scope
    FROM dbo.sentiment_action_items AS a
),
ranked AS (
    SELECT generation_id, stored_source_scope, stored_review_type_scope,
           ROW_NUMBER() OVER (PARTITION BY window_group, stored_source_scope,
                                           stored_review_type_scope
                              ORDER BY generated_at DESC, item_id DESC) AS newest
    FROM labelled
),
shown_scope AS (
    -- The newest generation of each window and stored scope, under its own
    -- scope...
    SELECT generation_id, stored_source_scope AS source_scope,
           stored_review_type_scope AS review_type_scope
    FROM ranked
    WHERE newest = 1
    UNION ALL
    -- ...Amazon / Product also as Amazon / All...
    SELECT generation_id, N'Amazon', N'All'
    FROM ranked
    WHERE newest = 1
      AND stored_source_scope = N'Amazon' AND stored_review_type_scope = N'Product'
    UNION ALL
    -- ...and Trustpilot / Service also as All / Service.
    SELECT generation_id, N'All', N'Service'
    FROM ranked
    WHERE newest = 1
      AND stored_source_scope = N'Trustpilot' AND stored_review_type_scope = N'Service'
)
SELECT
    l.window_group      AS window_label,
    l.window_months,
    x.source_scope,
    x.review_type_scope,
    l.is_placeholder,
    l.[rank]            AS item_rank,
    l.title,
    l.description,
    l.rationale,
    CASE WHEN l.related_area = 'overall' THEN 'Overall' ELSE l.related_area END
                        AS related_area,
    l.evidence,
    UPPER(LEFT(l.evidence_strength, 1)) + SUBSTRING(l.evidence_strength, 2, 20)
                        AS evidence_strength,
    CONCAT(CASE WHEN l.is_placeholder = 1 AND l.reviews_analysed = 0
                THEN N'Too few reviews for to-do items: none with a review date in this selection'
                WHEN l.is_placeholder = 1
                THEN CONCAT(N'Too few reviews for to-do items: ', l.reviews_analysed,
                            N' from ', CONVERT(CHAR(10), l.window_start, 23), N' to ',
                            CONVERT(CHAR(10), l.window_end, 23))
                ELSE CONCAT(N'Based on ', l.reviews_analysed, N' reviews from ',
                            CONVERT(CHAR(10), l.window_start, 23), N' to ',
                            CONVERT(CHAR(10), l.window_end, 23)) END,
           CASE WHEN l.window_end < DATEADD(DAY, -s.stale_after_days,
                                            CAST(SYSDATETIME() AS DATE))
                     AND NOT (l.is_placeholder = 1 AND l.reviews_analysed = 0)
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
JOIN shown_scope AS x
    ON x.generation_id = l.generation_id
CROSS JOIN settings AS s;
GO

-- ============================================================================
-- Verification -- run after the view above.
--
-- 1. One line per scope and window. After a run with the default settings,
--    expect 24 lines: 8 scopes (the 6 stored ones plus Amazon / All and
--    All / Service) x 3 windows. Each has generations = 1 and either
--    rows_shown = 3 with is_placeholder = 0 (ranks 1, 2, 3) or rows_shown = 1
--    with is_placeholder = 1 (too few reviews). Amazon / Service never
--    appears. Before the first run that saves scoped items, only All / All
--    appears (plus 'Custom window' if there are rows older than window_label).
-- ============================================================================
SELECT source_scope, review_type_scope, window_label,
       COUNT(*)                          AS rows_shown,
       MAX(CAST(is_placeholder AS INT))  AS is_placeholder,
       COUNT(DISTINCT generation_id)     AS generations,
       MIN(basis)                        AS basis,
       MAX(generated_at)                 AS generated_at
FROM dbo.vw_latest_action_items
GROUP BY source_scope, review_type_scope, window_label, window_months
ORDER BY source_scope, review_type_scope, window_months, window_label;

-- 2. The rows themselves: one row set per scope and window.
SELECT source_scope, review_type_scope, window_label, item_rank, is_placeholder,
       title, related_area, evidence_strength, basis, generated_at
FROM dbo.vw_latest_action_items
ORDER BY source_scope, review_type_scope, window_months, window_label, item_rank;
