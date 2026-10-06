-- ============================================================================
-- dbo.vw_review_analysis -- adds sector_group on top of everything already
-- built (resolved_sku, review_date_parsed, product_region). This is the full
-- view definition, not a diff -- CREATE OR ALTER replaces it in place.
--
-- === sector_group ===
-- Consolidates the LLM's open-vocabulary review_sector labels into readable,
-- de-duplicated groups for reporting, via dbo.sector_group_map (see
-- ../tables/sector_group_map.sql and ../seed/sector_group_map.sql for the
-- full rationale and the actual mapping).
--
-- Resolution order, per row:
--   1. review_type <> 'product'          -> NULL (service reviews were never
--                                            classified into a sector at all)
--   2. review_sector IS NULL or 'general' -> 'General / No Use Stated'
--                                            (no use stated -- structurally
--                                            different from an unmapped
--                                            label, so this is NOT looked up
--                                            in sector_group_map at all)
--   3. Matches a row in sector_group_map -> that group name
--   4. Anything else (a genuinely new label the pipeline has proposed since
--      sector_group_map was last updated) -> a humanised version of the raw
--      label (underscores -> spaces, first letter capitalised), so it still
--      shows up as its own readable row rather than disappearing or forcing
--      an "unmapped" bucket. Add a real row to sector_group_map once a new
--      label's pattern becomes clear, rather than leaving it on this
--      fallback indefinitely.
--
-- === product_region (revised in this update) ===
-- Two genuinely different reasons a review can end up with no region, kept
-- deliberately distinct rather than collapsed into one NULL:
--   1. resolved_sku IS NULL -- there's no product at all to derive a region
--      from (a service review, or a product review whose SKU never resolved
--      to begin with). Stays NULL here -- there's nothing to say.
--   2. resolved_sku IS NOT NULL but doesn't start with D/E/U/A -- a real,
--      successfully resolved SKU that just isn't a single-region product.
--      In practice this is almost entirely spare parts/accessories
--      (S-prefixed codes -- power adapters, cables) plus a handful of
--      legacy base product codes with no region letter at all (e.g.
--      "35500", the Lumi Task Lamp's un-prefixed catalogue entry). Verified
--      Trustpilot-only in practice -- Amazon's resolution path (the bridge
--      table, falling back to model_number) has always produced a
--      region-prefixed code so far (0 unresolved out of 666, vs. 117 of 872
--      on the Trustpilot side, as of this writing). This is real,
--      legitimate data, not a defect, and previously showed as a genuinely
--      blank, unlabeled column in Power BI's Matrix -- easy to mistake for
--      a rendering glitch. Now given an explicit label instead.
--
-- === review_text_display (added 2026-09-12) ===
-- Always-English display column for Power BI, so reviewers don't need to
-- read French/German/Italian/Spanish/Dutch/Swedish/Polish/Czech text
-- directly (Samir's ask -- see translation_lib.py for the translation pass
-- itself). COALESCE(review_text_en, review_text) -- not a CASE keyed off
-- detected_language -- so it degrades gracefully without extra branching:
-- English rows (review_text_en always NULL by design), successfully
-- translated rows, and rows not yet translated or where translation failed
-- (review_text_en still NULL either way) all fall through correctly to
-- *some* text rather than ever showing blank. detected_language is also
-- exposed directly, for filtering/flagging which rows are showing
-- translated vs. original-language text.
-- ============================================================================

CREATE OR ALTER VIEW dbo.vw_review_analysis AS
WITH amazon_sku_resolved AS (
    SELECT
        asin,
        MAX(product_code) AS product_code   -- any non-null value; regional
                                              -- variants are interchangeable
                                              -- for Family/Category purposes
    FROM dbo.amazon_asin_sku_map
    WHERE product_code IS NOT NULL
    GROUP BY asin
),
base AS (
    SELECT
        r.uid,
        r.review_id,
        r.review_type,
        r.review_source,
        r.rating,
        r.product_name,
        r.product_sku,
        r.product_asin,
        r.model_number,
        r.review_country,
        r.title,
        r.review_text,
        r.review_text_en,
        r.detected_language,
        r.reviewer_name,
        r.review_date,
        r.verified_purchase,
        r.sentiment,
        r.confidence,
        r.primary_sector AS review_sector,   -- renamed per decision #5 --
                                              -- avoids colliding with the
                                              -- existing SECTOR_FULL-based
                                              -- "Sector" filter already in
                                              -- the Power BI model
        r.primary_aspect,
        r.key_themes,
        r.analysis_error,
        r.loaded_at,
        sg.sector_group AS mapped_sector_group,   -- NULL if not in the table yet
        CASE
            WHEN r.review_type = 'product' AND r.review_source = 'trustpilot'
                THEN UPPER(LTRIM(RTRIM(r.product_sku)))
            WHEN r.review_type = 'product' AND r.review_source = 'amazon'
                THEN COALESCE(a.product_code, r.model_number)
            ELSE NULL
        END AS resolved_sku,
        CASE
            WHEN r.review_source = 'trustpilot'
                THEN TRY_CONVERT(DATE, r.review_date, 127)   -- 127 = ISO 8601
            WHEN r.review_source = 'amazon'
                THEN COALESCE(
                    TRY_PARSE(d.clean_date AS DATE USING 'en-US'),
                    TRY_PARSE(d.clean_date AS DATE USING 'de-DE'),
                    TRY_PARSE(d.clean_date AS DATE USING 'es-ES')
                )
            ELSE NULL
        END AS review_date_parsed
    FROM dbo.sentiment_analysis_reviews AS r
    LEFT JOIN amazon_sku_resolved AS a
        ON r.review_source = 'amazon' AND r.product_asin = a.asin
    LEFT JOIN dbo.sector_group_map AS sg
        ON r.primary_sector = sg.review_sector
    CROSS APPLY (
        -- Strips Amazon's "Reviewed in X on <date>" / German "Bewertet in X
        -- am <date>" boilerplate when concatenated directly onto the date
        -- with no separator, by cutting at whichever trigger phrase is
        -- found. No-op for rows that don't have it; safely NULL if
        -- review_date itself is NULL.
        SELECT LEFT(r.review_date,
            CASE
                WHEN CHARINDEX('Reviewed in', r.review_date) > 0
                    THEN CHARINDEX('Reviewed in', r.review_date) - 1
                WHEN CHARINDEX('Bewertet in', r.review_date) > 0
                    THEN CHARINDEX('Bewertet in', r.review_date) - 1
                ELSE LEN(ISNULL(r.review_date, ''))
            END) AS clean_date
    ) AS d
)
SELECT
    uid, review_id, review_type, review_source, rating, product_name,
    product_sku, product_asin, model_number, review_country, title,
    review_text, review_text_en, detected_language, reviewer_name, review_date,
    verified_purchase, sentiment, confidence, review_sector, primary_aspect,
    key_themes, analysis_error, loaded_at, resolved_sku, review_date_parsed,
    COALESCE(review_text_en, review_text) AS review_text_display,
    CASE
        WHEN resolved_sku IS NULL THEN NULL   -- no product at all (service
                                               -- review, or an unresolved
                                               -- product SKU) -- nothing to say
        WHEN LEFT(resolved_sku, 1) = 'D' THEN 'UK'
        WHEN LEFT(resolved_sku, 1) = 'E' THEN 'EU'
        WHEN LEFT(resolved_sku, 1) = 'U' THEN 'US'
        WHEN LEFT(resolved_sku, 1) = 'A' THEN 'Australia'
        ELSE 'No Region Data'   -- a real, resolved SKU that isn't a single-
                                 -- region product -- spare parts/accessories
                                 -- and legacy base codes; see header comment
    END AS product_region,
    CASE
        WHEN review_type <> 'product' THEN NULL
        WHEN review_sector IS NULL OR review_sector = 'general' THEN 'General / No Use Stated'
        ELSE COALESCE(
            mapped_sector_group,
            UPPER(LEFT(REPLACE(review_sector, '_', ' '), 1))
                + LOWER(SUBSTRING(REPLACE(review_sector, '_', ' '), 2, 200))
        )
    END AS sector_group
FROM base;
GO

-- ============================================================================
-- Verification -- run after the view above.
-- ============================================================================

-- sector_group: expect every group from sector_group_map's own verification
-- query, plus "General / No Use Stated" (760 as of this writing). Any raw,
-- un-humanised, underscore-containing value here is a genuinely new label
-- sector_group_map doesn't know about yet.
SELECT sector_group, COUNT(*) AS review_count
FROM dbo.vw_review_analysis
WHERE review_type = 'product'
GROUP BY sector_group
ORDER BY review_count DESC;

-- product_region: expect four real regions, a "No Region Data" row (~117,
-- Trustpilot-only per the header comment), and NULL only for rows with no
-- resolved_sku at all (service reviews, or any product review whose SKU
-- never resolved).
SELECT
    review_source,
    ISNULL(product_region, '(NULL -- no resolved_sku)') AS product_region,
    COUNT(*) AS review_count
FROM dbo.vw_review_analysis
WHERE review_type = 'product'
GROUP BY review_source, product_region
ORDER BY review_source, review_count DESC;

-- review_text_display: should never be NULL for a row that has review_text
-- at all (COALESCE guarantees a fallback to the original language). Also
-- shows how many rows are currently showing translated vs. original text.
SELECT
    COUNT(*) AS total_reviews,
    SUM(CASE WHEN review_text_display IS NULL AND review_text IS NOT NULL THEN 1 ELSE 0 END) AS unexpected_nulls,
    SUM(CASE WHEN detected_language IS NOT NULL THEN 1 ELSE 0 END) AS showing_translated_text
FROM dbo.vw_review_analysis;
