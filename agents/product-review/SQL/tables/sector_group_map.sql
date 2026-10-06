-- ============================================================================
-- dbo.sector_group_map
--
-- Consolidates the LLM's open-vocabulary review_sector labels into readable,
-- de-duplicated display groups for reporting (Power BI "Sector Share by SKU").
--
-- WHY THIS EXISTS AS ITS OWN TABLE, NOT A DAX CALCULATED COLUMN:
-- This exact grouping logic previously lived inside a Power BI-only DAX
-- SWITCH() column. That meant the business rule was invisible to anything
-- outside that one report file, and every new grouping decision required
-- opening Power BI to change it. Moving it here makes SQL the single source
-- of truth: visible to any tool that queries the database, and extendable
-- with a plain INSERT rather than a DAX edit + republish.
--
-- WHY OPEN-VOCABULARY LABELS NEEDED CONSOLIDATING AT ALL:
-- Because the LLM is free to invent a new label per review rather than reuse
-- an existing one (see multi_source_sentiment_pipeline.py's SECTORS design),
-- the same real-world use case can surface under several different exact
-- strings across different reviews -- e.g. "travel", "travel_use",
-- "motorhome_travel" are all genuinely the same finding, not three different
-- ones. Left ungrouped, a report listing every raw label produces ~40 rows
-- for a ~1,500-review catalogue, most showing a single review each -- noise
-- that buries the real signal rather than revealing it.
--
-- HOW GROUPS WERE CHOSEN (see 002_populate_sector_group_map.sql for the
-- actual mapping and per-group review counts):
--   - Genuinely repeated real-world use cases -> one named group each
--     (Travel, Music, Workshop & Garage, Model Making & Miniatures,
--      Home Office, Photography, Content Creation)
--   - True one-off findings with no natural sibling -> a single shared row,
--     "Other Specific Uses (single-review findings)" -- deliberately worded
--     differently from "Unclassified" so it reads as "real but rare
--     findings", not "the AI failed to classify these"
--   - Samir's original six seeded categories (sewing_needlecraft,
--     art_painting, reading_vision_wellbeing, beauty, medical,
--     jewellery_trade) map 1:1 to his own naming, UNCHANGED. Deliberately:
--     no new LLM-proposed label is merged into any of his six, even where
--     one is thematically close (e.g. "printmaking" is NOT folded into
--     "Art & Painting") -- extending his defined taxonomy without his
--     sign-off isn't this table's job. New labels only ever get grouped
--     with other new labels.
--   - "general" (no use stated at all) is NOT included in this table -- it's
--     a structurally different case (absence of a use, not a use that needs
--     naming) and is handled directly in the view via COALESCE, matching
--     how it's already excluded from every "% of stated use" measure.
--
-- WHAT HAPPENS WHEN A BRAND-NEW LABEL SHOWS UP IN A FUTURE PIPELINE RUN,
-- BEFORE ANYONE HAS ADDED IT HERE:
-- The view (003) falls back to showing it as its own readable row (a
-- humanised version of the raw label), NOT swept into "Other Specific
-- Uses". That bucket should only ever contain labels a person has actually
-- looked at and decided are true singles -- never a silent catch-all for
-- "not mapped yet". Add a row here whenever a new label's pattern becomes
-- clear (e.g. once a second "xyz_travel"-shaped label shows up).
-- ============================================================================

IF OBJECT_ID('dbo.sector_group_map', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.sector_group_map (
        review_sector  NVARCHAR(50)   NOT NULL PRIMARY KEY,  -- raw LLM output, e.g. "sewing_needlecraft"
        sector_group   NVARCHAR(100)  NOT NULL,               -- display grouping, e.g. "Sewing & Needlecraft"
        notes          NVARCHAR(500)  NULL,
        updated_at     DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO
