-- ============================================================================
-- Populates dbo.sector_group_map.
--
-- Safe to re-run: uses MERGE, not INSERT, so running this again later (e.g.
-- after editing a grouping below, or adding new rows to this same script)
-- updates/inserts only the rows listed here and never touches any row added
-- directly via SSMS in between runs.
--
-- Source: dbo.vw_review_analysis, review_type='product', GROUP BY
-- review_sector -- captured and verified against the full 1,538-review
-- product catalogue at the time this was written. Every raw label from that
-- query is accounted for below (see 001's header comment for the grouping
-- rationale); total reconciles exactly to 1,538 across all groups + general.
-- ============================================================================

MERGE dbo.sector_group_map AS target
USING (VALUES
    -- Samir's original six seeded categories -- unchanged, 1:1, per decision #5/#taxonomy work.
    ('sewing_needlecraft',       'Sewing & Needlecraft',                        'Samir seeded category (unchanged)'),
    ('art_painting',             'Art & Painting',                              'Samir seeded category (unchanged)'),
    ('reading_vision_wellbeing', 'Reading, Vision & Wellbeing',                 'Samir seeded category (unchanged)'),
    ('beauty',                   'Beauty',                                      'Samir seeded category (unchanged)'),
    ('medical',                  'Medical',                                     'Samir seeded category (unchanged)'),
    ('jewellery_trade',          'Jewellery & Trade',                           'Samir seeded category (unchanged)'),

    -- Travel -- 10 reviews total across 7 raw labels, all genuinely the same finding.
    ('travel',                   'Travel', NULL),
    ('travel_portable_use',      'Travel', NULL),
    ('travel_use',               'Travel', NULL),
    ('travel_work',              'Travel', NULL),
    ('travel_productivity',      'Travel', NULL),
    ('travel_portable',          'Travel', NULL),
    ('motorhome_travel',         'Travel', NULL),

    -- Music -- 7 reviews across 4 raw labels.
    ('music_reading',            'Music', NULL),
    ('music_performance',        'Music', NULL),
    ('music_lessons',            'Music', NULL),
    ('music_piano',              'Music', NULL),

    -- Workshop & Garage -- 6 reviews across 6 raw labels.
    ('workbench_electronics',    'Workshop & Garage', NULL),
    ('workbench_workshop',       'Workshop & Garage', NULL),
    ('machining_workshop',       'Workshop & Garage', NULL),
    ('garage_workshop',          'Workshop & Garage', NULL),
    ('hobby_workshop',           'Workshop & Garage', NULL),
    ('woodworking',              'Workshop & Garage', 'Thematically workshop-adjacent, grouped in'),

    -- Model Making & Miniatures -- 5 reviews across 3 raw labels.
    ('miniature_painting',       'Model Making & Miniatures', NULL),
    ('model_making',             'Model Making & Miniatures', NULL),
    ('lego_building',            'Model Making & Miniatures', NULL),

    -- Home Office -- 10 reviews across 5 raw labels.
    ('home_office',              'Home Office', NULL),
    ('desk_work',                'Home Office', NULL),
    ('office_work',              'Home Office', NULL),
    ('studying_working',         'Home Office', NULL),
    ('video_conferencing',       'Home Office', 'Remote-work tool use, grouped with Home Office'),

    -- Photography -- 8 reviews across 2 raw labels.
    ('photography',              'Photography', NULL),
    ('photo_printing',           'Photography', NULL),

    -- Content Creation -- 3 reviews across 2 raw labels.
    ('content_creation',         'Content Creation', NULL),
    ('video_production',         'Content Creation', 'Closely overlaps content_creation, grouped in'),

    -- Other Specific Uses -- 9 true one-offs, no natural sibling identified.
    -- Deliberately NOT merged into any Samir-seeded category even where
    -- thematically close (e.g. printmaking / art_painting, eyewear_repair /
    -- jewellery_trade) -- see 001's header comment for why.
    ('computing_gaming',         'Other Specific Uses (single-review findings)', NULL),
    ('electronics_art',          'Other Specific Uses (single-review findings)', NULL),
    ('bedside_lamp',             'Other Specific Uses (single-review findings)', NULL),
    ('home_decor',               'Other Specific Uses (single-review findings)', NULL),
    ('home_lighting',            'Other Specific Uses (single-review findings)', NULL),
    ('eyewear_repair',           'Other Specific Uses (single-review findings)', 'Not merged into jewellery_trade -- deliberate'),
    ('cooking_food_prep',        'Other Specific Uses (single-review findings)', NULL),
    ('printmaking',              'Other Specific Uses (single-review findings)', 'Not merged into art_painting -- deliberate'),
    ('presentations_teaching',   'Other Specific Uses (single-review findings)', NULL)

) AS source (review_sector, sector_group, notes)
ON target.review_sector = source.review_sector
WHEN MATCHED THEN
    UPDATE SET sector_group = source.sector_group,
               notes = source.notes,
               updated_at = SYSUTCDATETIME()
WHEN NOT MATCHED THEN
    INSERT (review_sector, sector_group, notes)
    VALUES (source.review_sector, source.sector_group, source.notes);
GO

-- ============================================================================
-- Verification -- run after the MERGE above.
-- Expect: 44 rows (every raw label except "general", which this table
-- deliberately excludes), grouped into 14 display groups.
-- ============================================================================
SELECT COUNT(*) AS total_mapped_labels FROM dbo.sector_group_map;

SELECT sector_group, COUNT(*) AS raw_labels_in_group
FROM dbo.sector_group_map
GROUP BY sector_group
ORDER BY raw_labels_in_group DESC;
