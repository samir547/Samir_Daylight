"""Business to-do items: window selection, the summary the model is shown,
the prompt, validation of the reply, and the orchestration with a fake
analyzer and a fake repository (no API or database access)."""
import json
import re
from datetime import date, datetime, timedelta

import pytest

from daylight_sentiment.application.action_items import (
    ACTION_ITEMS_MAX_TOKENS,
    generate_action_items,
)
from daylight_sentiment.config import ActionItemSettings
from daylight_sentiment.domain import taxonomy
from daylight_sentiment.domain.action_items import (
    ASPECT_NAMES,
    BANNED_WORDS,
    ActionItemsValidationError,
    FIELD_MAX_CHARS,
    MIN_TREND_COUNT,
    MIN_WINDOW_REVIEWS,
    SMALL_SAMPLE_REVIEWS,
    STRONG_ACTION_VERBS,
    TREND_THEMES,
    ReviewWindow,
    aspect_name,
    banned_wording,
    build_summary,
    build_trend,
    count_negative_themes,
    field_identifiers,
    format_action_items,
    months_before,
    theme_key,
    parse_themes,
    products_most_negative,
    recent_part_start,
    select_excerpts,
    select_windows,
    shorten,
    single_spaced,
    small_sample_areas,
    summary_areas,
    summary_numbers,
    title_verb,
    unsupported_numbers,
    validate_action_items,
    window_label,
)
from daylight_sentiment.domain.action_items_prompt import (
    build_action_items_prompt,
    build_retry_prompt,
)
from daylight_sentiment.infra.analyzers import FatalProviderError, SentimentAnalyzer


def review(i, day, sentiment="positive", review_type="product", **extra):
    row = {
        "uid": f"trustpilot:{review_type}:{i:04d}",
        "review_type": review_type,
        "review_source": "trustpilot",
        "sentiment": sentiment,
        "confidence": 0.9,
        "rating": 1 if sentiment == "negative" else 5,
        "sector_group": "Sewing & Needlecraft" if review_type == "product" else None,
        "primary_aspect": "shipping_quality" if review_type == "service" else None,
        "product_region": "UK" if review_type == "product" else None,
        "key_themes": '["brightness"]',
        "review_date_parsed": day,
        "review_text_en": None,
        "review_text": f"review text {i}" if sentiment == "negative" else None,
    }
    row.update(extra)
    return row


def window_of(rows, months=12):
    dates = [r["review_date_parsed"] for r in rows]
    return ReviewWindow(months, rows, min(dates), max(dates))


TODAY = date(2026, 10, 24)


def windows_by_months(rows, months=(3, 6, 12), today=TODAY, **kw):
    return {w.months: w for w in select_windows(rows, months, today=today, **kw)}


class TestSelectWindows:
    def test_end_is_newest_review_date_not_today(self):
        rows = [review(i, date(2024, 3, 1 + i % 28)) for i in range(120)]
        [w] = select_windows(rows, [12], today=TODAY)
        assert w.end == date(2024, 3, 28)
        assert w.start == date(2023, 3, 29)
        assert not w.skipped and w.review_count == 120

    def test_starts_for_3_6_and_12_months(self):
        rows = [review(i, date(2026, 8, 20)) for i in range(150)]
        w = windows_by_months(rows)
        assert {m: (w[m].start, w[m].end) for m in w} == {
            3: (date(2026, 5, 21), date(2026, 8, 20)),
            6: (date(2026, 2, 21), date(2026, 8, 20)),
            12: (date(2025, 8, 21), date(2026, 8, 20)),
        }
        assert [x.label for x in select_windows(rows, (3, 6, 12), today=TODAY)] == [
            "Last 3 months", "Last 6 months", "Last 12 months"]

    def test_each_window_holds_only_its_own_months(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(100)]
                + [review(1000 + i, date(2026, 4, 1)) for i in range(60)]
                + [review(2000 + i, date(2025, 10, 1)) for i in range(40)]
                + [review(3000 + i, date(2024, 1, 1)) for i in range(5)])
        w = windows_by_months(rows)
        assert (w[3].review_count, w[6].review_count, w[12].review_count) == (100, 160, 200)
        for x in w.values():
            assert all(r["review_date_parsed"] >= x.start for r in x.rows)

    def test_boundary_day_is_in_the_window(self):
        rows = ([review(i, date(2026, 8, 20)) for i in range(100)]
                + [review(1000, date(2025, 8, 20)), review(2000, date(2025, 8, 21))])
        [w] = select_windows(rows, [12], today=TODAY)
        assert w.start == date(2025, 8, 21) and w.review_count == 101

    def test_thin_window_is_skipped_not_widened(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(60)]
                + [review(1000 + i, date(2026, 3, 1)) for i in range(60)])
        w = windows_by_months(rows)
        assert w[3].skipped and not w[6].skipped and not w[12].skipped
        assert w[3].review_count == 60  # its own months only, not every review
        assert w[3].start == date(2026, 5, 2)
        assert w[3].skip_reason == (
            "only 60 analysed reviews from 2026-05-02 to 2026-08-01 (minimum 100)")

    @pytest.mark.parametrize("count, skipped", [(99, True), (100, False)])
    def test_minimum_is_100_reviews(self, count, skipped):
        assert MIN_WINDOW_REVIEWS == 100
        rows = [review(i, date(2026, 8, 1)) for i in range(count)]
        [w] = select_windows(rows, [3], today=TODAY)
        assert w.skipped is skipped

    def test_every_window_skipped_when_data_is_thin(self):
        rows = [review(i, date(2026, 8, 1)) for i in range(40)]
        assert all(w.skipped for w in select_windows(rows, (3, 6, 12), today=TODAY))

    def test_undated_and_unanalysed_rows_are_excluded_and_counted(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(100)]
                + [review(500 + i, None) for i in range(7)]
                + [review(900, "not a date")]
                + [review(950, date(2026, 8, 1), sentiment=None)])
        [w] = select_windows(rows, [12], today=TODAY)
        assert w.review_count == 100
        assert w.undated_excluded == 8

    def test_no_dated_reviews_skips_every_window(self):
        windows = select_windows([review(1, None)], (3, 12), today=TODAY)
        assert [w.months for w in windows] == [3, 12]
        assert all(w.skipped and w.undated_excluded == 1 for w in windows)

    def test_accepts_datetimes_and_iso_strings(self):
        rows = ([review(i, datetime(2026, 8, 1, 12)) for i in range(60)]
                + [review(100 + i, "2026-07-01") for i in range(60)])
        [w] = select_windows(rows, [12], today=TODAY)
        assert w.review_count == 120 and w.end == date(2026, 8, 1)

    def test_future_date_does_not_move_the_windows_past_today(self):
        rows = ([review(i, date(2026, 8, 20)) for i in range(120)]
                + [review(500, date(2027, 5, 1))])
        w = windows_by_months(rows)
        assert {x.end for x in w.values()} == {TODAY}
        assert w[12].start == date(2025, 10, 25) and w[3].start == date(2026, 7, 25)
        assert w[12].capped_from == date(2027, 5, 1) and w[12].future_dated == 1
        assert w[12].review_count == 121  # the future-dated review is kept

    def test_no_cap_when_the_newest_date_is_not_in_the_future(self):
        rows = [review(i, TODAY) for i in range(120)]
        [w] = select_windows(rows, [12], today=TODAY)
        assert w.end == TODAY
        assert w.capped_from is None and w.future_dated == 0

    def test_defaults_to_the_current_date(self):
        future = date.today() + timedelta(days=400)
        rows = [review(i, future) for i in range(120)]
        [w] = select_windows(rows, [12])
        assert w.end == date.today() and w.start <= w.end

    def test_months_before_clamps_short_months(self):
        assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
        assert months_before(date(2026, 1, 15), 12) == date(2025, 1, 15)

    def test_window_label(self):
        assert window_label(3) == "Last 3 months"
        assert window_label(12) == "Last 12 months"
        assert window_label(1) == "Last 1 month"
        assert len(window_label(255)) <= 30  # window_label NVARCHAR(30)


class TestParseThemes:
    @pytest.mark.parametrize("raw, expected", [
        ('["Brightness", "build_quality"]', ["brightness", "build quality"]),
        ('["Brightness", "brightness  "]', ["brightness"]),
        ("[\"cracked arm\", 'stand", ["cracked arm", "stand"]),
        ("not json, at all", ["not json", "at all"]),
        ('"single theme"', ["single theme"]),
        ('{"a": 1}', []),
        ('[1, null, "ok"]', ["ok"]),
        ("", []),
        (None, []),
    ])
    def test_parses_defensively(self, raw, expected):
        assert parse_themes(raw) == expected


class TestBuildSummary:
    def rows(self):
        d = date(2026, 6, 15)
        return [
            review(1, d, "negative", key_themes='["Flicker", "build_quality"]'),
            review(2, d, "negative", key_themes="garbage, Flicker"),
            review(3, d, "positive"),
            review(4, date(2026, 5, 2), "neutral",
                   sector_group="Art & Painting", product_region="EU"),
            review(5, d, "negative", review_type="service", review_source="trustpilot",
                   key_themes='["late delivery"]'),
            review(6, date(2026, 5, 2), "positive", review_type="service",
                   primary_aspect="customer_service"),
        ]

    def test_counts_and_shares(self):
        s = build_summary(window_of(self.rows()), 15, 10, 300)
        assert s["window"] == {"label": "Last 12 months", "start": "2026-05-02",
                               "end": "2026-06-15", "reviews": 6}
        assert s["sentiment"] == {"reviews": 6, "positive": 2, "negative": 3,
                                  "neutral": 1, "negative_share_pct": 50.0}
        sectors = {e["name"]: e for e in s["product_by_sector_group"]}
        assert sectors["Sewing & Needlecraft"] == {
            "name": "Sewing & Needlecraft", "reviews": 3, "negatives": 2,
            "negative_share_pct": 66.7, "small_sample": True}
        assert sectors["Art & Painting"]["negatives"] == 0
        aspects = {e["name"]: e for e in s["service_by_aspect"]}
        assert aspects["Shipping quality"]["negative_share_pct"] == 100.0
        assert set(aspects) == {"Shipping quality", "Customer service"}
        assert {e["name"] for e in s["product_by_region"]} == {"UK", "EU"}
        assert s["by_review_type_and_source"] == [
            {"review_type": "product", "review_source": "trustpilot",
             "reviews": 4, "negatives": 2, "negative_share_pct": 50.0, "small_sample": True},
            {"review_type": "service", "review_source": "trustpilot",
             "reviews": 2, "negatives": 1, "negative_share_pct": 50.0, "small_sample": True},
        ]
        assert s["negative_reviews_total"] == 3
        assert s["small_sample_threshold_reviews"] == SMALL_SAMPLE_REVIEWS == 30

    def test_negative_themes_counted_and_normalised(self):
        s = build_summary(window_of(self.rows()), 15, 10, 300)
        themes = {t["theme"]: t["count"] for t in s["top_negative_themes"]}
        assert themes == {"flicker": 2, "build quality": 1, "garbage": 1,
                          "late delivery": 1}
        assert s["top_negative_themes"][0] == {"theme": "flicker", "count": 2}
        # positive reviews' themes ("brightness") aren't counted
        assert "brightness" not in themes

    def test_top_themes_limit(self):
        rows = [review(i, date(2026, 6, 1), "negative", key_themes=json.dumps([f"t{i}"]))
                for i in range(30)]
        assert len(build_summary(window_of(rows), 15, 10, 300)["top_negative_themes"]) == 15

    def test_monthly_trend(self):
        s = build_summary(window_of(self.rows()), 15, 10, 300)
        assert s["monthly_negative"] == [
            {"month": "2026-05", "reviews": 2, "negatives": 0, "negative_share_pct": 0.0,
             "small_sample": True},
            {"month": "2026-06", "reviews": 4, "negatives": 3, "negative_share_pct": 75.0,
             "small_sample": True},
        ]

    def test_no_region_section_without_regions(self):
        rows = [review(i, date(2026, 6, 1), product_region=None) for i in range(3)]
        s = build_summary(window_of(rows), 15, 10, 300)
        assert "product_by_region" not in s
        assert "product_by_region_and_source" not in s

    def test_no_reviewer_names_or_contact_details_in_output(self):
        rows = self.rows()
        for r in rows:
            r["reviewer_name"] = "Jane Q Reviewer"
        rows[0]["review_text"] = ("Lamp broke. Email me at jane.q@example.com "
                                  "or call +44 7700 900123.")
        s = build_summary(window_of(rows), 15, 10, 300)
        dumped = json.dumps(s)
        assert "Jane Q Reviewer" not in dumped
        assert "reviewer_name" not in dumped
        assert "jane.q@example.com" not in dumped
        assert "7700 900123" not in dumped
        assert "uid" not in dumped

    def test_summary_is_json_serialisable(self):
        json.dumps(build_summary(window_of(self.rows()), 15, 10, 300))


# Product reviews by region and source in the real data (since 2025-08-12):
# (region, source, reviews, negatives). Within Amazon the UK is the worst
# region; the UK only looks best overall because most of its reviews are
# from Trustpilot.
REAL_REGION_SOURCE = [
    ("EU", "amazon", 186, 29), ("EU", "trustpilot", 17, 1),
    ("UK", "amazon", 45, 10), ("UK", "trustpilot", 77, 2),
    ("US", "amazon", 87, 17), ("US", "trustpilot", 37, 4),
    ("No Region Data", "trustpilot", 14, 2),
]


def real_region_source_rows():
    rows, i = [], 0
    for region, source, reviews, negatives in REAL_REGION_SOURCE:
        for k in range(reviews):
            rows.append(review(i, date(2026, 6, 1), "negative" if k < negatives else "positive",
                               review_source=source, product_region=region))
            i += 1
    return rows


class TestRegionAndSource:
    def test_region_by_source_table_from_the_real_numbers(self):
        s = build_summary(window_of(real_region_source_rows()), 15, 10, 300)
        table = {(e["region"], e["review_source"]): e for e in s["product_by_region_and_source"]}
        assert table == {
            ("EU", "amazon"): {"region": "EU", "review_source": "amazon", "reviews": 186,
                               "negatives": 29, "negative_share_pct": 15.6,
                               "small_sample": False},
            ("EU", "trustpilot"): {"region": "EU", "review_source": "trustpilot",
                                   "reviews": 17, "negatives": 1,
                                   "negative_share_pct": 5.9, "small_sample": True},
            ("UK", "amazon"): {"region": "UK", "review_source": "amazon", "reviews": 45,
                               "negatives": 10, "negative_share_pct": 22.2,
                               "small_sample": False},
            ("UK", "trustpilot"): {"region": "UK", "review_source": "trustpilot",
                                   "reviews": 77, "negatives": 2,
                                   "negative_share_pct": 2.6, "small_sample": False},
            ("US", "amazon"): {"region": "US", "review_source": "amazon", "reviews": 87,
                               "negatives": 17, "negative_share_pct": 19.5,
                               "small_sample": False},
            ("US", "trustpilot"): {"region": "US", "review_source": "trustpilot",
                                   "reviews": 37, "negatives": 4,
                                   "negative_share_pct": 10.8, "small_sample": False},
            ("No Region Data", "trustpilot"): {
                "region": "No Region Data", "review_source": "trustpilot", "reviews": 14,
                "negatives": 2, "negative_share_pct": 14.3, "small_sample": True},
        }
        # The mixed-source region totals stay a separate section.
        regions = {e["name"]: e for e in s["product_by_region"]}
        assert regions["UK"]["reviews"] == 122 and regions["UK"]["negatives"] == 12
        assert s["negative_reviews_total"] == 65

    def test_sections_are_listed_in_a_stable_order(self):
        s = build_summary(window_of(real_region_source_rows()), 15, 10, 300)
        assert [(e["region"], e["review_source"]) for e in s["product_by_region_and_source"]] == [
            ("EU", "amazon"), ("EU", "trustpilot"), ("No Region Data", "trustpilot"),
            ("UK", "amazon"), ("UK", "trustpilot"), ("US", "amazon"), ("US", "trustpilot")]


class TestSmallSample:
    @pytest.mark.parametrize("reviews, small", [(29, True), (30, False), (31, False)])
    def test_flag_at_the_threshold(self, reviews, small):
        rows = [review(i, date(2026, 6, 1), "negative" if i < 5 else "positive",
                       resolved_sku="D1", review_source="amazon")
                for i in range(reviews)]
        s = build_summary(window_of(rows), 15, 10, 300)
        for section in ("product_by_sector_group", "product_by_region",
                        "product_by_region_and_source", "by_review_type_and_source",
                        "products_most_negative"):
            [entry] = s[section]
            assert entry["small_sample"] is small, section
            assert entry["reviews"] == reviews and entry["negatives"] == 5

    def test_threshold_can_be_passed_in(self):
        rows = [review(i, date(2026, 6, 1)) for i in range(10)]
        s = build_summary(window_of(rows), 15, 10, 300, small_sample_reviews=10)
        assert s["product_by_sector_group"][0]["small_sample"] is False
        assert s["small_sample_threshold_reviews"] == 10


class TestProducts:
    def rows(self):
        rows, i = [], 0
        # (sku, name, reviews, negatives)
        for sku, name, reviews, negatives in [
                ("D35500", "Lumi Task Lamp", 40, 6), ("U31000", "Halo Floor Lamp", 12, 3),
                ("E20000", "Slimline Magnifier", 50, 2), (None, "No SKU lamp", 20, 10)]:
            for k in range(reviews):
                rows.append(review(i, date(2026, 6, 1),
                                   "negative" if k < negatives else "positive",
                                   resolved_sku=sku, product_name=name,
                                   product_region={"D": "UK", "U": "US", "E": "EU"}.get(
                                       (sku or "x")[0])))
                i += 1
        return rows

    def test_most_negative_products_with_flags(self):
        products = build_summary(window_of(self.rows()), 15, 10, 300)["products_most_negative"]
        assert products == [
            {"product": "Lumi Task Lamp", "sku": "D35500", "region": "UK",
             "reviews_by_source": {"trustpilot": 40}, "reviews": 40,
             "negatives": 6, "negative_share_pct": 15.0, "small_sample": False},
            {"product": "Halo Floor Lamp", "sku": "U31000", "region": "US",
             "reviews_by_source": {"trustpilot": 12}, "reviews": 12,
             "negatives": 3, "negative_share_pct": 25.0, "small_sample": True},
        ]

    def test_reviews_by_source_per_product(self):
        rows = ([review(i, date(2026, 6, 1), "negative", resolved_sku="D1",
                        review_source="amazon") for i in range(7)]
                + [review(100 + i, date(2026, 6, 1), resolved_sku="D1") for i in range(3)])
        [p] = products_most_negative(rows, 30)
        assert p["reviews_by_source"] == {"amazon": 7, "trustpilot": 3}

    def test_products_with_fewer_than_3_negatives_or_no_sku_left_out(self):
        skus = {p["sku"] for p in products_most_negative(self.rows(), 30)}
        assert "E20000" not in skus  # only 2 negatives
        assert None not in skus and "" not in skus

    def test_top_10_only(self):
        rows = [review(i, date(2026, 6, 1), "negative", resolved_sku=f"D{i // 3:03d}")
                for i in range(3 * 12)]
        assert len(products_most_negative(rows, 30)) == 10

    def test_name_is_most_frequent_spelling_and_cut_short(self):
        long_name = "Daylight " + "very long marketing title " * 10
        rows = ([review(i, date(2026, 6, 1), "negative", resolved_sku="D1",
                        product_name=long_name) for i in range(3)]
                + [review(9, date(2026, 6, 1), "negative", resolved_sku="D1",
                          product_name="Other")])
        [p] = products_most_negative(rows, 30)
        assert p["product"].startswith("Daylight very long") and p["product"].endswith("...")
        assert len(p["product"]) <= 80

    def test_falls_back_to_the_sku_without_a_name(self):
        rows = [review(i, date(2026, 6, 1), "negative", resolved_sku="D7", product_name=None)
                for i in range(3)]
        assert products_most_negative(rows, 30)[0]["product"] == "D7"


class TestThemeNormalisation:
    @pytest.mark.parametrize("a, b", [
        ("product defect", "defective product"),
        ("product defect", "product defects"),
        ("broken arm", "arm broken"),
        ("battery", "batteries"),
        ("delivery delay", "delivery delays"),
    ])
    def test_variants_share_a_key(self, a, b):
        assert theme_key(a) == theme_key(b)

    @pytest.mark.parametrize("a, b", [
        ("product failure", "product defect"),
        ("short lifespan", "product failure"),
        ("brightness", "bright"),
        ("glass", "glas"),
    ])
    def test_different_themes_stay_apart(self, a, b):
        assert theme_key(a) != theme_key(b)

    def test_merged_and_counted_once_per_review(self):
        d = date(2026, 6, 1)
        rows = [review(1, d, "negative", key_themes='["product defect", "defective product"]'),
                review(2, d, "negative", key_themes='["Defective product"]'),
                review(3, d, "negative", key_themes='["defective product", "short lifespan"]'),
                review(4, d, "positive", key_themes='["product defect"]')]
        assert count_negative_themes(rows) == [("defective product", 3), ("short lifespan", 1)]


def test_aspect_names_cover_the_taxonomy():
    assert set(ASPECT_NAMES) == set(taxonomy.ASPECTS)
    assert aspect_name("returns_refunds") == "Returns & refunds"
    assert aspect_name("shipping_quality") == "Shipping quality"
    assert aspect_name("new_label_x") == "New label x"
    assert aspect_name(None) == "Unknown"


class TestSelectExcerpts:
    def test_prefers_english_and_truncates(self):
        rows = [review(1, date(2026, 6, 1), "negative", review_text="Texte original " * 50,
                       review_text_en="English text " * 50)]
        [ex] = select_excerpts(rows, 10, 300)
        assert ex["text"].startswith("English text")
        assert len(ex["text"]) <= 300 and ex["text"].endswith(" English...")

    def test_quote_is_cut_at_a_word_boundary(self):
        text = "Please send me the return authorization number so I can post it back today"
        rows = [review(1, date(2026, 6, 1), "negative", review_text=text)]
        [ex] = select_excerpts(rows, 10, 45)
        assert ex["text"] == "Please send me the return authorization..."
        assert len(ex["text"]) <= 45

    @pytest.mark.parametrize("text, limit, expected", [
        ("short enough", 20, "short enough"),
        ("one two three four", 12, "one two..."),
        ("one two, three", 12, "one two..."),
        ("one two three", 11, "one two..."),
        ("abcdefghijklmnop", 10, "abcdefg..."),
    ])
    def test_shorten(self, text, limit, expected):
        assert shorten(text, limit) == expected
        assert len(shorten(text, limit)) <= limit

    def test_falls_back_to_original_text(self):
        rows = [review(1, date(2026, 6, 1), "negative", review_text="Lampe cassée")]
        assert select_excerpts(rows, 10, 300)[0]["text"] == "Lampe cassée"

    def test_only_negatives_with_text(self):
        rows = [review(1, date(2026, 6, 1), "positive", review_text="great"),
                review(2, date(2026, 6, 1), "negative", review_text="  ")]
        assert select_excerpts(rows, 10, 300) == []

    def test_most_confident_spread_across_areas_and_deterministic(self):
        rows = []
        for i in range(8):
            rows.append(review(i, date(2026, 6, 1), "negative", confidence=0.99 - i / 100))
        rows.append(review(100, date(2026, 6, 1), "negative", sector_group="Beauty",
                           confidence=0.5))
        rows.append(review(200, date(2026, 6, 1), "negative", review_type="service",
                           confidence=0.6))
        picked = select_excerpts(rows, 3, 300)
        assert [p["area"] for p in picked] == [
            "Sewing & Needlecraft", "Shipping quality", "Beauty"]
        assert picked[0]["text"] == "review text 0"  # most confident in its area
        assert select_excerpts(list(reversed(rows)), 3, 300) == picked

    def test_identical_complaints_quoted_once(self):
        rows = [review(1, date(2026, 6, 1), "negative", review_text="Arm  snapped."),
                review(2, date(2026, 6, 1), "negative", review_text="arm snapped."),
                review(3, date(2026, 6, 1), "negative", review_text="Bulb died.")]
        assert [e["text"] for e in select_excerpts(rows, 10, 300)] == [
            "Arm snapped.", "Bulb died."]

    def test_respects_max_excerpts(self):
        rows = [review(i, date(2026, 6, 1), "negative") for i in range(25)]
        assert len(select_excerpts(rows, 10, 300)) == 10


def good_reply():
    return {"items": [
        {"rank": 2, "title": "Review the stand design", "description": "Do it.",
         "rationale": "12 of 40 negative reviews.", "related_area": "Sewing & Needlecraft",
         "evidence": "12 negative reviews mention the stand.",
         "evidence_strength": "Moderate"},
        {"rank": 1, "title": "Monitor delivery packaging", "description": "Do it.",
         "rationale": "9 of 10.", "related_area": "SHIPPING QUALITY",
         "evidence": "9 service reviews.", "evidence_strength": "early  signal"},
        {"rank": 3, "title": "Check flicker reports", "description": "Do it.",
         "rationale": "Only 4 reviews, so this is tentative.", "related_area": "overall",
         "evidence": "4 negative reviews mention flicker.", "evidence_strength": "strong"},
    ]}


def summary_for(sector_groups=(("Sewing & Needlecraft", False),),
                aspects=(("Shipping quality", True),), regions=()):
    """The parts of a summary the validator reads: (name, small_sample)."""
    def section(entries):
        return [{"name": n, "reviews": 10 if small else 100, "small_sample": small}
                for n, small in entries]
    return {"small_sample_threshold_reviews": 30,
            # The figures good_reply() quotes.
            "by_review_type_and_source": [
                {"reviews": 40, "negatives": 12, "negative_share_pct": 30.0},
                {"reviews": 10, "negatives": 9, "negative_share_pct": 90.0},
                {"reviews": 4, "negatives": 4, "negative_share_pct": 15.6}],
            "product_by_sector_group": section(sector_groups),
            "service_by_aspect": section(aspects),
            "product_by_region": section(regions)}


AREAS = summary_for()


class TestValidate:
    def test_accepts_good_reply_sorted_and_canonicalised(self):
        items = validate_action_items(good_reply(), AREAS)
        assert [i["rank"] for i in items] == [1, 2, 3]
        assert items[0]["related_area"] == "Shipping quality"
        assert [i["evidence_strength"] for i in items] == ["early signal", "moderate", "strong"]

    def test_strips_whitespace(self):
        reply = good_reply()
        reply["items"][0]["title"] = "  Review the stand design \n"
        assert validate_action_items(reply, AREAS)[1]["title"] == "Review the stand design"

    def test_line_breaks_and_whitespace_runs_become_one_space(self):
        # A line break inside a JSON string survives parsing; whatever shows
        # the text later may drop it and join the words ("thatreturn").
        reply = good_reply()
        reply["items"][0]["description"] = ("Check the products that\nreturn most often,"
                                            "\r\n\tand  which\n\nspecific parts fail.")
        reply["items"][0]["title"] = "Review the\nstand design"
        item = validate_action_items(reply, AREAS)[1]
        assert item["description"] == ("Check the products that return most often, and "
                                       "which specific parts fail.")
        assert item["title"] == "Review the stand design"
        assert single_spaced("a  b\n") == "a b"

    @pytest.mark.parametrize("count", [2, 4])
    def test_rejects_wrong_item_count(self, count):
        reply = good_reply()
        items = reply["items"]
        reply["items"] = items[:2] if count == 2 else items + [dict(items[0], rank=4)]
        with pytest.raises(ActionItemsValidationError, match="expected 3 items"):
            validate_action_items(reply, AREAS)

    def test_rejects_duplicate_ranks(self):
        reply = good_reply()
        reply["items"][2]["rank"] = 1
        with pytest.raises(ActionItemsValidationError, match="ranks"):
            validate_action_items(reply, AREAS)

    @pytest.mark.parametrize("rank", ["1", 1.0, True, None, 0])
    def test_rejects_bad_rank(self, rank):
        reply = good_reply()
        reply["items"][1]["rank"] = rank
        with pytest.raises(ActionItemsValidationError):
            validate_action_items(reply, AREAS)

    @pytest.mark.parametrize("name", ["title", "description", "rationale",
                                      "related_area", "evidence", "evidence_strength"])
    def test_rejects_missing_or_empty_field(self, name):
        reply = good_reply()
        del reply["items"][0][name]
        with pytest.raises(ActionItemsValidationError, match=name):
            validate_action_items(reply, AREAS)
        reply = good_reply()
        reply["items"][0][name] = "   "
        with pytest.raises(ActionItemsValidationError, match=name):
            validate_action_items(reply, AREAS)

    def test_rejects_non_string_field(self):
        reply = good_reply()
        reply["items"][0]["evidence"] = 12
        with pytest.raises(ActionItemsValidationError, match="evidence"):
            validate_action_items(reply, AREAS)

    def test_rejects_unknown_related_area(self):
        reply = good_reply()
        reply["items"][0]["related_area"] = "Underwater Basket Weaving"
        with pytest.raises(ActionItemsValidationError, match="related_area"):
            validate_action_items(reply, AREAS)

    @pytest.mark.parametrize("name", sorted(FIELD_MAX_CHARS))
    def test_rejects_over_long_fields(self, name):
        reply = good_reply()
        reply["items"][0][name] = "x" * (FIELD_MAX_CHARS[name] + 1)
        with pytest.raises(ActionItemsValidationError, match="limit"):
            validate_action_items(reply, AREAS)

    def test_db_column_sizes_bound_the_caps(self):
        assert FIELD_MAX_CHARS["title"] <= 200
        assert FIELD_MAX_CHARS["related_area"] <= 100
        assert FIELD_MAX_CHARS["evidence_strength"] <= 20

    def test_accepts_overall_and_readable_aspect_names_in_any_case(self):
        summary = summary_for(aspects=[(n, False) for n in ASPECT_NAMES.values()])
        for area in ["Overall", "OVERALL", "Returns & refunds", "returns & REFUNDS",
                     "Delivery speed"]:
            reply = good_reply()
            reply["items"][0]["related_area"] = area
            items = validate_action_items(reply, summary)
            assert items[1]["related_area"] in ["overall"] + list(ASPECT_NAMES.values())

    def test_rejects_raw_aspect_label(self):
        reply = good_reply()
        reply["items"][0]["related_area"] = "returns_refunds"
        summary = summary_for(aspects=[("Returns & refunds", False)])
        with pytest.raises(ActionItemsValidationError, match="returns_refunds"):
            validate_action_items(reply, summary)

    @pytest.mark.parametrize("strength", ["weak", "high", "early", "very strong"])
    def test_rejects_evidence_strength_outside_the_allowed_set(self, strength):
        reply = good_reply()
        reply["items"][2]["evidence_strength"] = strength
        with pytest.raises(ActionItemsValidationError, match="evidence_strength"):
            validate_action_items(reply, AREAS)

    @pytest.mark.parametrize("strength", ["strong", "moderate"])
    def test_small_sample_area_must_be_an_early_signal(self, strength):
        reply = good_reply()
        reply["items"][1]["evidence_strength"] = strength
        with pytest.raises(ActionItemsValidationError,
                           match="Shipping quality.*fewer than 30 reviews.*early signal"):
            validate_action_items(reply, AREAS)

    def test_small_sample_rule_uses_the_summary_flags(self):
        summary = build_summary(window_of(TestBuildSummary().rows()), 15, 10, 300)
        assert set(small_sample_areas(summary)) == set(summary_areas(summary)) - {"overall"}
        assert small_sample_areas(summary_for()) == ["Shipping quality"]

    def test_lists_every_problem_found(self):
        reply = good_reply()
        reply["items"][0]["related_area"] = "Atlantis"
        reply["items"][2]["evidence_strength"] = "huge"
        with pytest.raises(ActionItemsValidationError) as exc:
            validate_action_items(reply, AREAS)
        assert "item 1 related_area 'Atlantis'" in str(exc.value)
        assert "item 3 evidence_strength 'huge'" in str(exc.value)

    @pytest.mark.parametrize("reply", [None, [], {}, {"items": "x"}, {"items": [1, 2, 3]}])
    def test_rejects_malformed_reply(self, reply):
        with pytest.raises(ActionItemsValidationError):
            validate_action_items(reply, AREAS)


class TestNumberCheck:
    def known(self):
        return summary_numbers(summary_for())

    def test_accepts_numbers_from_the_summary(self):
        text = ("12 of 40 reviews (30.0%) and 9 of 10 (90%); 15.6% and 15.6 per cent; "
                "about 16% when rounded.")
        assert unsupported_numbers(text, self.known()) == []

    def test_thousands_separator(self):
        known = summary_numbers({"reviews": 1234})
        assert unsupported_numbers("1,234 reviews", known) == []

    def test_rejects_an_invented_percentage(self):
        assert unsupported_numbers("47.3% of buyers complain", self.known()) == ["47.3%"]

    def test_rejects_a_number_not_in_the_summary(self):
        assert unsupported_numbers("99 reviews mention it", self.known()) == ["99"]

    def test_a_count_is_not_a_percentage(self):
        # 40 is a count in the summary, never a share.
        assert unsupported_numbers("40% are negative", self.known()) == ["40%"]

    def test_rounding_only_to_one_decimal_or_a_whole_number(self):
        assert unsupported_numbers("15.60%", self.known()) == []
        assert unsupported_numbers("15.5%", self.known()) == ["15.5%"]
        assert unsupported_numbers("15%", self.known()) == ["15%"]

    @pytest.mark.parametrize("text", [
        "Reviews from 2025-08-12 to 2026-08-11",
        "since August 2025", "in 2026", "on 12 August 2025", "on August 12, 2025",
        "on 12/08/2025", "over the last 18 months", "within 3 weeks",
        "product D35500 and E1234",
    ])
    def test_ignores_dates_years_periods_and_codes(self, text):
        assert unsupported_numbers(text, self.known()) == []

    def test_numbers_inside_summary_text_count(self):
        summary = {"negative_excerpts": [{"text": "It broke after 3 uses, 50% dimmer"}],
                   "window": {"start": "2025-08-12"}}
        known = summary_numbers(summary)
        assert unsupported_numbers("3 uses and 50%", known) == []
        assert unsupported_numbers("8 uses", known) == ["8"]

    def test_bools_are_not_numbers(self):
        assert summary_numbers({"small_sample": True}).values == set()

    @pytest.mark.parametrize("field", ["description", "rationale", "evidence"])
    def test_reply_with_an_unsupported_number_fails_and_lists_it(self, field):
        reply = good_reply()
        reply["items"][0][field] = "Around 47.3% of buyers, 99 reviews, since 2025."
        with pytest.raises(ActionItemsValidationError,
                           match=rf"item 1 {field} has numbers that are not in the summary: "
                                 r"47\.3%, 99"):
            validate_action_items(reply, AREAS)

    def test_title_is_not_number_checked(self):
        reply = good_reply()
        reply["items"][0]["title"] = "Review the 5 worst products"
        validate_action_items(reply, AREAS)


class TestPrompt:
    def test_includes_summary_areas_and_rules(self):
        rows = TestBuildSummary().rows()
        summary = build_summary(window_of(rows), 15, 10, 300)
        areas = summary_areas(summary)
        prompt = build_action_items_prompt(summary, areas)
        assert json.dumps(summary, indent=2, ensure_ascii=False) in prompt
        assert "covers 6\nreviews dated 2026-05-02 to 2026-06-15" in prompt
        assert '"overall", "Sewing & Needlecraft", "Art & Painting"' in prompt
        for rule in ("Financial Director", "British English", "EXACTLY 3",
                     "starts with a verb", "ONLY the numbers", "cite the numbers",
                     "too thin", "generic advice", '"rank"', '"title"', '"description"',
                     '"rationale"', '"related_area"', '"evidence"',
                     '"evidence_strength"', '"early signal"'):
            assert rule in " ".join(prompt.split()), rule

    def test_related_area_placeholder_points_at_its_rule(self):
        summary = build_summary(window_of(TestBuildSummary().rows()), 15, 10, 300)
        prompt = build_action_items_prompt(summary, summary_areas(summary))
        [n] = re.findall(r'"related_area": "<one value from rule (\d+)>"', prompt)
        assert re.search(rf'^{n}\. "related_area" must be exactly one of', prompt, re.M)

    def test_real_numbers_prompt_has_the_like_for_like_and_small_sample_rules(self):
        summary = build_summary(window_of(real_region_source_rows()), 15, 10, 300)
        prompt = build_action_items_prompt(summary, summary_areas(summary))
        assert '"product_by_region_and_source": [' in prompt
        assert '"region": "UK",\n      "review_source": "amazon",\n      "reviews": 45,' in prompt
        flat = " ".join(prompt.split())
        for rule in (
                "Compare like with like. Never compare regions, review websites or any "
                "other groups whose reviews come from the review websites in different "
                "proportions, except within the same website",
                "most Trustpilot reviews are invited by the company and tend to be more "
                "positive",
                "A difference between Amazon and Trustpilot is background only, never "
                "evidence of a problem by itself.",
                'An item based mainly on a group marked "small_sample": true must ask the '
                "business to monitor or investigate, never to overhaul or fix",
                "Never claim that a difference is statistically significant, meaningful, "
                '"not random"',
                'Never add the counts in "top_negative_themes" together',
                'use "negative_reviews_total"',
                'name them (from "products_most_negative")',
                'Use "overall" for an issue that spans several',
                'Use a catch-all group such as "General / No Use Stated" only as rule 14 '
                "allows."):
            assert rule in flat, rule

    def test_summary_areas(self):
        summary = build_summary(window_of(TestBuildSummary().rows()), 15, 10, 300)
        assert summary_areas(summary) == [
            "overall", "Sewing & Needlecraft", "Art & Painting",
            "Shipping quality", "Customer service", "UK", "EU"]


def year_window(rows, end=date(2026, 10, 24)):
    """A 12-month window from 2025-10-25 to ``end``; its recent third starts
    on 2026-06-26."""
    return ReviewWindow(12, rows, date(2025, 10, 25), end)


def trend_rows():
    """Sewing: 60 earlier reviews (30 negative), 13 recent (4 negative).
    Art: 40 earlier (2 negative), 5 recent (3 negative)."""
    rows, i = [], 0
    for group, day, reviews, negatives, themes in [
            ("Sewing & Needlecraft", date(2026, 1, 10), 60, 30, '["flicker"]'),
            ("Sewing & Needlecraft", date(2026, 9, 1), 13, 4, '["flicker", "glare"]'),
            ("Art & Painting", date(2026, 2, 1), 40, 2, '["glare"]'),
            ("Art & Painting", date(2026, 9, 20), 5, 3, '["glare"]')]:
        for k in range(reviews):
            rows.append(review(i, day, "negative" if k < negatives else "positive",
                               sector_group=group, key_themes=themes))
            i += 1
    return rows


class TestTrend:
    def test_recent_part_is_the_last_third_of_the_window(self):
        assert recent_part_start(year_window([])) == date(2026, 6, 26)
        w3 = ReviewWindow(3, [], date(2026, 7, 25), date(2026, 10, 24))
        assert recent_part_start(w3) == date(2026, 9, 25)  # 92 days -> the last 30
        tiny = ReviewWindow(1, [], date(2026, 10, 23), date(2026, 10, 24))
        assert recent_part_start(tiny) == date(2026, 10, 24)

    def test_parts_and_sector_group_counts_shares_and_flags(self):
        trend = build_summary(year_window(trend_rows()), 15, 10, 300)["trend"]
        assert trend["recent_part"] == {"start": "2026-06-26", "end": "2026-10-24",
                                        "reviews": 18}
        assert trend["earlier_part"] == {"start": "2025-10-25", "end": "2026-06-25",
                                         "reviews": 100}
        sectors = {e["name"]: e for e in trend["product_by_sector_group"]}
        assert sectors["Sewing & Needlecraft"] == {
            "name": "Sewing & Needlecraft",
            "recent": {"reviews": 13, "negatives": 4, "negative_share_pct": 30.8,
                       "small_sample": True},
            "earlier": {"reviews": 60, "negatives": 30, "negative_share_pct": 50.0,
                        "small_sample": False},
            "few_mentions": False}
        assert sectors["Art & Painting"]["recent"] == {
            "reviews": 5, "negatives": 3, "negative_share_pct": 60.0, "small_sample": True}
        assert sectors["Art & Painting"]["earlier"]["negative_share_pct"] == 5.0
        # Same groups, same order, as the main section.
        main = build_summary(year_window(trend_rows()), 15, 10, 300)["product_by_sector_group"]
        assert [e["name"] for e in trend["product_by_sector_group"]] == [e["name"] for e in main]

    def test_sources_regions_and_aspects(self):
        rows = trend_rows() + [
            review(500 + k, date(2026, 9, 5), "negative" if k < 2 else "positive",
                   review_type="service", primary_aspect="returns_refunds")
            for k in range(4)]
        rows += [review(600 + k, date(2026, 3, 1), "negative", review_source="amazon",
                        product_region="EU") for k in range(3)]
        trend = build_summary(year_window(rows), 15, 10, 300)["trend"]
        sources = {(e["review_type"], e["review_source"]): e
                   for e in trend["by_review_type_and_source"]}
        assert sources[("service", "trustpilot")]["recent"] == {
            "reviews": 4, "negatives": 2, "negative_share_pct": 50.0, "small_sample": True}
        assert sources[("service", "trustpilot")]["earlier"]["reviews"] == 0
        assert sources[("product", "amazon")]["earlier"]["negatives"] == 3
        [aspect] = trend["service_by_aspect"]
        assert aspect["name"] == "Returns & refunds" and aspect["recent"]["negatives"] == 2
        regions = {(e["region"], e["review_source"]): e
                   for e in trend["product_by_region_and_source"]}
        assert regions[("EU", "amazon")]["earlier"]["reviews"] == 3
        assert regions[("UK", "trustpilot")]["recent"]["reviews"] == 18

    def test_negative_themes_per_part_as_a_share_of_all_reviews(self):
        trend = build_summary(year_window(trend_rows()), 15, 10, 300)["trend"]
        themes = {e["theme"]: e for e in trend["top_negative_themes"]}
        assert themes["flicker"] == {
            "theme": "flicker",
            "recent": {"reviews": 18, "negatives_mentioning": 4,
                       "share_of_reviews_pct": 22.2, "small_sample": True},
            "earlier": {"reviews": 100, "negatives_mentioning": 30,
                        "share_of_reviews_pct": 30.0, "small_sample": False},
            "few_mentions": False}
        assert themes["glare"]["recent"]["negatives_mentioning"] == 7
        assert themes["glare"]["earlier"]["negatives_mentioning"] == 2

    def test_at_most_ten_themes(self):
        rows = [review(i, date(2026, 9, 1), "negative", key_themes=json.dumps([f"t{i}"]))
                for i in range(30)]
        trend = build_summary(year_window(rows), 15, 10, 300)["trend"]
        assert len(trend["top_negative_themes"]) == TREND_THEMES == 10

    def test_window_with_a_tiny_recent_part(self):
        recent_start = date(2026, 6, 26)
        earlier = [r for r in trend_rows() if r["review_date_parsed"] < recent_start]
        recent = [r for r in trend_rows() if r["review_date_parsed"] >= recent_start]
        trend = build_summary(year_window(earlier + recent[:2]), 15, 10, 300)["trend"]
        assert trend["recent_part"]["reviews"] == 2
        sewing = {e["name"]: e for e in trend["product_by_sector_group"]}["Sewing & Needlecraft"]
        assert sewing["recent"] == {"reviews": 2, "negatives": 2, "negative_share_pct": 100.0,
                                    "small_sample": True}
        assert sewing["earlier"]["small_sample"] is False
        art = {e["name"]: e for e in trend["product_by_sector_group"]}["Art & Painting"]
        assert art["recent"] == {"reviews": 0, "negatives": 0, "negative_share_pct": 0.0,
                                 "small_sample": True}
        flicker = {e["theme"]: e for e in trend["top_negative_themes"]}["flicker"]
        assert flicker["recent"] == {"reviews": 2, "negatives_mentioning": 2,
                                     "share_of_reviews_pct": 100.0, "small_sample": True}

    def test_future_dated_reviews_count_as_recent(self):
        rows = trend_rows() + [review(900, date(2026, 12, 1), "negative")]
        trend = build_summary(year_window(rows), 15, 10, 300)["trend"]
        assert trend["recent_part"]["reviews"] == 19

    def test_trend_numbers_are_accepted_by_the_validator(self):
        summary = build_summary(year_window(trend_rows()), 15, 10, 300)
        text = ("Sewing: 4 of 13 recent reviews negative (30.8%) against 30 of 60 earlier "
                "(50.0%); flicker in 22.2% of recent reviews.")
        assert unsupported_numbers(text, summary_numbers(summary)) == []
        del summary["trend"]
        assert {"30.8%", "22.2%"} <= set(unsupported_numbers(text, summary_numbers(summary)))


def by_source_rows():
    rows, i = [], 0
    for group, source, reviews, negatives in [
            ("Sewing & Needlecraft", "amazon", 40, 10),
            ("Sewing & Needlecraft", "trustpilot", 20, 1),
            ("General / No Use Stated", "trustpilot", 35, 7)]:
        for k in range(reviews):
            rows.append(review(i, date(2026, 6, 1), "negative" if k < negatives else "positive",
                               sector_group=group, review_source=source))
            i += 1
    for aspect, source, reviews, negatives in [("customer_service", "trustpilot", 12, 6),
                                               ("customer_service", "amazon", 3, 1),
                                               ("returns_refunds", "trustpilot", 8, 1)]:
        for k in range(reviews):
            rows.append(review(i, date(2026, 6, 1), "negative" if k < negatives else "positive",
                               review_type="service", review_source=source,
                               primary_aspect=aspect))
            i += 1
    return rows


class TestGroupBySource:
    def test_sector_group_by_source(self):
        s = build_summary(window_of(by_source_rows()), 15, 10, 300)
        assert s["product_by_sector_group_and_source"] == [
            {"sector_group": "General / No Use Stated", "review_source": "trustpilot",
             "reviews": 35, "negatives": 7, "negative_share_pct": 20.0, "small_sample": False},
            {"sector_group": "Sewing & Needlecraft", "review_source": "amazon",
             "reviews": 40, "negatives": 10, "negative_share_pct": 25.0, "small_sample": False},
            {"sector_group": "Sewing & Needlecraft", "review_source": "trustpilot",
             "reviews": 20, "negatives": 1, "negative_share_pct": 5.0, "small_sample": True},
        ]

    def test_aspect_by_source(self):
        s = build_summary(window_of(by_source_rows()), 15, 10, 300)
        assert s["service_by_aspect_and_source"] == [
            {"aspect": "Customer service", "review_source": "amazon", "reviews": 3,
             "negatives": 1, "negative_share_pct": 33.3, "small_sample": True},
            {"aspect": "Customer service", "review_source": "trustpilot", "reviews": 12,
             "negatives": 6, "negative_share_pct": 50.0, "small_sample": True},
            {"aspect": "Returns & refunds", "review_source": "trustpilot", "reviews": 8,
             "negatives": 1, "negative_share_pct": 12.5, "small_sample": True},
        ]

    def test_small_sample_threshold_applies(self):
        s = build_summary(window_of(by_source_rows()), 15, 10, 300, small_sample_reviews=20)
        flags = {(e["sector_group"], e["review_source"]): e["small_sample"]
                 for e in s["product_by_sector_group_and_source"]}
        assert flags[("Sewing & Needlecraft", "trustpilot")] is False

    def test_by_source_figures_are_accepted_by_the_validator(self):
        summary = build_summary(window_of(by_source_rows()), 15, 10, 300)
        text = ("Within Trustpilot, Sewing has 1 of 20 negative (5.0%) and customer service "
                "6 of 12 (50.0%).")
        assert unsupported_numbers(text, summary_numbers(summary)) == []
        for key in ("product_by_sector_group_and_source", "service_by_aspect_and_source"):
            del summary[key]
        assert {"5.0%", "50.0%"} <= set(unsupported_numbers(text, summary_numbers(summary)))


class TestNewPromptRules:
    def prompt_raw(self):
        summary = build_summary(window_of(by_source_rows()), 15, 10, 300)
        return build_action_items_prompt(summary, summary_areas(summary))

    def prompt(self):
        return " ".join(self.prompt_raw().split())

    def test_like_for_like_extends_to_sector_groups_aspects_and_products(self):
        flat = self.prompt()
        assert ("The same applies to sector groups, service aspects and products: compare "
                "them only within the same review website (use "
                '"product_by_sector_group_and_source", "service_by_aspect_and_source" and '
                '"reviews_by_source"), or say plainly that their mix of review websites '
                "differs and treat the gap as background only.") in flat
        assert ("A gap between groups whose mix of review websites differs must never be the "
                "main reason for an item.") in flat
        assert '"product_by_sector_group_and_source": [' in self.prompt_raw()

    def test_catch_all_rule(self):
        flat = self.prompt()
        assert ('14. Catch-all groups are not findings. Groups such as "General / No Use '
                'Stated", "Unknown", "No Region Data"') in flat
        assert ("Never write an item whose only action is to investigate or monitor such a "
                "group.") in flat
        assert ("Use one only when the summary shows the cause inside it (particular "
                "products, topics or aspects), and name that cause.") in flat

    def test_evidence_strength_follows_the_evidence(self):
        flat = self.prompt()
        assert ("If the key figures behind an item (the products, groups or aspects it rests "
                'on) are mostly marked "small_sample": true, use "early signal", even when '
                '"related_area" is "overall".') in flat
        assert ('Use "strong" only when the figures come from large groups and the same '
                "pattern shows in more than one group, or in both the recent and the earlier "
                'part of "trend".') in flat

    def test_trend_rule(self):
        flat = self.prompt()
        assert ("If an issue's recent negative share is clearly lower than its earlier share, "
                'and the recent part is not marked "small_sample": true, say that it may '
                "already be improving, and rank it lower or say so plainly.") in flat
        assert "If the recent share is clearly higher, say that it is getting worse." in flat
        assert ('Never say that an issue is "fixed", "solved" or "resolved": reviews cannot '
                "prove that.") in flat
        assert '"trend": {' in self.prompt_raw()

    def test_strong_on_a_small_sample_area_is_still_rejected(self):
        summary = build_summary(window_of(by_source_rows()), 15, 10, 300)
        assert "Customer service" in small_sample_areas(summary)
        reply = {"items": [
            {"rank": r, "title": "Check refunds", "description": "Do it.",
             "rationale": "6 of 12.", "related_area": area, "evidence": "6 of 12.",
             "evidence_strength": "strong"}
            for r, area in ((1, "Customer service"), (2, "overall"), (3, "overall"))]}
        with pytest.raises(ActionItemsValidationError, match="Customer service.*early signal"):
            validate_action_items(reply, summary)


def test_format_action_items():
    rows = [review(i, date(2026, 6, 1)) for i in range(3)]
    text = format_action_items(validate_action_items(good_reply(), AREAS), window_of(rows))
    assert text.startswith("Last 12 months: top 3 to-do items -- Based on 3 reviews from "
                           "2026-06-01 to 2026-06-01")
    assert "1. Monitor delivery packaging  [Shipping quality]" in text
    assert "   Evidence strength: early signal" in text


# -- orchestration ------------------------------------------------------------

class FakeAnalyzer(SentimentAnalyzer):
    """Returns ``reply`` to every call, or ``replies`` in turn."""
    def __init__(self, reply=None, error=None, replies=None):
        self.replies = list(replies) if replies is not None else None
        self.reply = reply
        self.error = error
        self.calls = []

    def complete_json(self, prompt, max_tokens=None):
        self.calls.append((prompt, max_tokens))
        if self.error:
            raise self.error
        if self.replies is not None:
            return self.replies.pop(0)
        return self.reply

    def analyze_product_review(self, text, product_name=None, rating=None):
        raise AssertionError("not used")

    def analyze_service_review(self, text, rating=None):
        raise AssertionError("not used")


class FakeRepository:
    def __init__(self, rows):
        self.rows = rows
        self.saved = []

    def fetch_analysed_rows(self):
        return self.rows

    def save(self, items, window_label, window_months, window_start, window_end,
             reviews_analysed, llm_provider, llm_model, run_timestamp=None):
        self.saved.append(dict(items=items, window_label=window_label,
                               window_months=window_months, window_start=window_start,
                               window_end=window_end, reviews_analysed=reviews_analysed,
                               llm_provider=llm_provider, llm_model=llm_model,
                               run_timestamp=run_timestamp))
        return f"generation-{len(self.saved)}"


def dataset(n=120):
    rows = []
    for i in range(n):
        rows.append(review(i, date(2026, 8, 20), "negative" if i % 3 == 0 else "positive"))
    rows.append(review(9000, date(2026, 8, 20), "negative", review_type="service"))
    return rows


def dataset_reply():
    """A valid reply for dataset(): 40 of the 120 product reviews are
    negative (33.3%); the 1 service review is negative."""
    reply = good_reply()
    for item in reply["items"]:
        item["rationale"] = "40 of 120 product reviews are negative (33.3%)."
        item["evidence"] = "40 negative reviews; 1 service review."
    return reply


class TestGenerate:
    def run(self, reply=None, error=None, rows=None, dry_run=False, replies=None,
            settings=None):
        analyzer = FakeAnalyzer(reply if reply is not None else dataset_reply(), error,
                                replies=replies)
        repo = FakeRepository(rows if rows is not None else dataset())
        outcome = generate_action_items(analyzer, repo, settings or ActionItemSettings(),
                                        "claude", "model-x",
                                        run_timestamp="2026-10-24T10:00:00",
                                        dry_run=dry_run, today=TODAY)
        return outcome, analyzer, repo

    def test_one_call_and_one_saved_set_per_window(self):
        outcome, analyzer, repo = self.run()
        assert len(analyzer.calls) == 3  # valid first time: no retries
        assert {c[1] for c in analyzer.calls} == {ACTION_ITEMS_MAX_TOKENS}
        assert [(s["window_label"], s["window_months"], s["window_start"]) for s in repo.saved] == [
            ("Last 3 months", 3, date(2026, 5, 21)),
            ("Last 6 months", 6, date(2026, 2, 21)),
            ("Last 12 months", 12, date(2025, 8, 21))]
        for saved in repo.saved:
            assert [i["rank"] for i in saved["items"]] == [1, 2, 3]
            assert saved["window_end"] == date(2026, 8, 20)
            assert saved["reviews_analysed"] == 121
            assert (saved["llm_provider"], saved["llm_model"]) == ("claude", "model-x")
            assert saved["run_timestamp"] == "2026-10-24T10:00:00"
        ids = [w.generation_id for w in outcome.windows]
        assert len(set(ids)) == 3 and None not in ids
        assert outcome.failed == [] and len(outcome.generated) == 3

    def test_each_prompt_is_built_from_its_own_window(self):
        rows = dataset() + [review(1000 + i, date(2026, 1, 10)) for i in range(30)]
        _, analyzer, _ = self.run(rows=rows)
        prompts = [c[0] for c in analyzer.calls]
        assert "covers 121\nreviews dated 2026-05-21 to 2026-08-20 (last 3 months)" in prompts[0]
        assert "covers 151\nreviews dated 2025-08-21 to 2026-08-20 (last 12 months)" in prompts[2]

    def test_number_check_uses_that_windows_summary(self):
        # 150 reviews exist only in the 12-month window, so a reply citing it
        # passes there and is retried (then fails) in the shorter windows.
        rows = dataset() + [review(1000 + i, date(2026, 1, 10)) for i in range(30)]
        reply = dataset_reply()
        for item in reply["items"]:
            item["rationale"] = "40 negative product reviews."
            item["evidence"] = "150 product reviews in total."
        outcome, analyzer, repo = self.run(rows=rows, reply=reply)
        assert [s["window_label"] for s in repo.saved] == ["Last 12 months"]
        assert [w.window.label for w in outcome.failed] == ["Last 3 months", "Last 6 months"]
        assert "150" in str(outcome.failed[0].error)
        assert len(analyzer.calls) == 5  # 2 + 2 + 1

    def test_dry_run_saves_nothing(self):
        outcome, analyzer, repo = self.run(dry_run=True)
        assert [len(w.items) for w in outcome.windows] == [3, 3, 3]
        assert all(w.generation_id is None for w in outcome.windows)
        assert repo.saved == []

    def test_one_failing_window_does_not_stop_the_others(self):
        bad = {"items": []}
        replies = [dataset_reply(), bad, bad, dataset_reply()]
        outcome, analyzer, repo = self.run(replies=replies)
        assert [s["window_label"] for s in repo.saved] == ["Last 3 months", "Last 12 months"]
        [failed] = outcome.failed
        assert failed.window.label == "Last 6 months" and failed.items == []
        assert isinstance(failed.error, ActionItemsValidationError)
        assert failed.generation_id is None

    def test_a_save_error_fails_only_that_window(self):
        class FlakyRepository(FakeRepository):
            def save(self, items, window_label, *args, **kwargs):
                if window_label == "Last 3 months":
                    raise RuntimeError("connection dropped")
                return super().save(items, window_label, *args, **kwargs)

        repo = FlakyRepository(dataset())
        outcome = generate_action_items(FakeAnalyzer(dataset_reply()), repo,
                                        ActionItemSettings(), "claude", "m", today=TODAY)
        assert [w.window.label for w in outcome.failed] == ["Last 3 months"]
        assert [s["window_label"] for s in repo.saved] == ["Last 6 months", "Last 12 months"]

    def generate(self, analyzer, repo, dry_run=False, months=(12,)):
        return generate_action_items(analyzer, repo, ActionItemSettings(windows_months=months),
                                     "claude", "m", dry_run=dry_run, today=TODAY)

    def test_invalid_then_valid_reply_is_saved_once(self, caplog):
        bad = dataset_reply()
        bad["items"][0]["rationale"] = "47.3% of buyers complain."
        analyzer = FakeAnalyzer(replies=[bad, dataset_reply()])
        repo = FakeRepository(dataset())
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome = self.generate(analyzer, repo)
        assert len(analyzer.calls) == 2 and len(repo.saved) == 1
        assert [i["rank"] for i in outcome.windows[0].items] == [1, 2, 3]
        first, retry = analyzer.calls[0][0], analyzer.calls[1][0]
        assert retry.startswith(first)
        assert ("Your previous reply failed because: item 1 rationale has numbers that are "
                "not in the summary: 47.3%") in retry
        assert analyzer.calls[1][1] == ACTION_ITEMS_MAX_TOKENS
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "Last 12 months" in warnings[0].getMessage()
        assert "retrying once" in warnings[0].getMessage()
        assert "47.3%" in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    def test_both_replies_invalid_saves_nothing(self, caplog):
        analyzer = FakeAnalyzer(replies=[{"items": []}, {"items": [1, 2, 3]}])
        repo = FakeRepository(dataset())
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome = self.generate(analyzer, repo)
        [failed] = outcome.failed
        assert isinstance(failed.error, ActionItemsValidationError)
        assert "item 1 is not an object" in str(failed.error)
        assert len(analyzer.calls) == 2 and repo.saved == []
        assert "expected 3 items, got 0" in analyzer.calls[1][0]
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2
        assert "retrying once: expected 3 items, got 0" in warnings[0]
        assert "the retry failed validation too: item 1 is not an object" in warnings[1]
        assert all(r.exc_info is None for r in caplog.records)

    def test_both_invalid_in_a_dry_run_saves_nothing(self):
        analyzer = FakeAnalyzer(replies=[{"items": []}, {"items": []}])
        repo = FakeRepository(dataset())
        outcome = self.generate(analyzer, repo, dry_run=True)
        assert len(outcome.failed) == 1 and repo.saved == []

    def test_provider_error_is_not_retried_and_stops_every_window(self):
        analyzer = FakeAnalyzer(error=FatalProviderError("bad key"))
        repo = FakeRepository(dataset())
        with pytest.raises(FatalProviderError):
            self.generate(analyzer, repo, months=(3, 6, 12))
        assert len(analyzer.calls) == 1 and repo.saved == []

    def test_retry_prompt(self):
        assert build_retry_prompt("PROMPT", "x; y") == (
            "PROMPT\n\nYour previous reply failed because: x; y\n\nWrite the 3 items "
            "again, following every rule above, and respond ONLY with\nJSON in the same "
            "shape.")

    def test_thin_window_is_skipped_without_calling_the_model(self, caplog):
        rows = ([review(i, date(2026, 8, 20)) for i in range(60)]
                + [review(1000 + i, date(2026, 4, 1)) for i in range(60)]
                + [review(9000, date(2026, 8, 20), "negative", review_type="service")])
        reply = dataset_reply()
        for item in reply["items"]:
            item["rationale"] = item["evidence"] = "Most reviews are positive."
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome, analyzer, repo = self.run(rows=rows, reply=reply)
        assert [w.skipped for w in outcome.windows] == [True, False, False]
        assert outcome.windows[0].items == [] and outcome.failed == []
        assert len(analyzer.calls) == 2
        assert [s["window_label"] for s in repo.saved] == ["Last 6 months", "Last 12 months"]
        assert ("Action items for Last 3 months: skipped -- only 61 analysed reviews"
                in caplog.text)

    def test_all_windows_skipped_saves_nothing_and_is_not_a_failure(self, caplog):
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome, analyzer, repo = self.run(rows=dataset(10))
        assert all(w.skipped for w in outcome.windows)
        assert outcome.failed == [] and outcome.generated == []
        assert analyzer.calls == [] and repo.saved == []
        assert caplog.text.count("skipped") == 3

    def test_window_cap_is_logged_once(self, caplog):
        future = TODAY + timedelta(days=30)
        rows = dataset() + [review(9999, future, review_type="service")]
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome, _, repo = self.run(rows=rows)
        assert {s["window_end"] for s in repo.saved} == {TODAY}
        assert outcome.windows[0].window.capped_from == future
        assert caplog.text.count("is after today, so the windows end today") == 1
        assert "1 review(s) dated after today" in caplog.text

    def test_fetch_error_propagates(self):
        class BrokenRepository(FakeRepository):
            def fetch_analysed_rows(self):
                raise RuntimeError("Invalid object name 'dbo.vw_review_analysis'")

        with pytest.raises(RuntimeError):
            generate_action_items(FakeAnalyzer(dataset_reply()), BrokenRepository([]),
                                  ActionItemSettings(), "claude", "m", today=TODAY)

    def test_prompt_is_built_from_the_summary_only(self):
        rows = dataset()
        for r in rows:
            r["reviewer_name"] = "Private Person"
        _, analyzer, _ = self.run(rows=rows)
        for prompt, _ in analyzer.calls:
            assert "Private Person" not in prompt
            assert '"reviews": 121' in prompt


# -- wording, title verb and trend reliability checks --------------------------

class TestTitleVerb:
    def reply(self, title, strength="early signal"):
        reply = good_reply()
        reply["items"][1]["title"] = title  # rank 1, related_area Shipping quality
        reply["items"][1]["evidence_strength"] = strength
        if strength != "early signal":
            reply["items"][1]["related_area"] = "overall"
        return reply

    @pytest.mark.parametrize("title", ["Fix delivery packaging", "fix: delivery packaging",
                                       "  Overhaul packaging", "RESOLVE damage",
                                       "Eliminate dents", "Stop the damage", "Eradicate dents"])
    def test_strong_verb_on_an_early_signal_is_rejected(self, title):
        with pytest.raises(ActionItemsValidationError) as exc:
            validate_action_items(self.reply(title), AREAS)
        assert "item 2 is an 'early signal' but its title starts with" in str(exc.value)
        assert "start it with Monitor, Investigate or Review" in str(exc.value)

    @pytest.mark.parametrize("title", ["Monitor delivery packaging", "Investigate dents",
                                       "Review packaging", "Fixing is premature: monitor"])
    def test_other_verbs_are_accepted_for_an_early_signal(self, title):
        items = validate_action_items(self.reply(title), AREAS)
        assert items[0]["title"] == title

    @pytest.mark.parametrize("strength", ["moderate", "strong"])
    def test_does_not_apply_to_moderate_or_strong(self, strength):
        items = validate_action_items(self.reply("Fix delivery packaging", strength), AREAS)
        assert items[0]["title"] == "Fix delivery packaging"

    def test_the_verb_list(self):
        assert STRONG_ACTION_VERBS == ("fix", "overhaul", "resolve", "eliminate", "stop",
                                       "eradicate")
        assert title_verb("  Fix: the stand") == "fix" and title_verb("") == ""


class TestBannedWording:
    @pytest.mark.parametrize("text, found", [
        ("A significant rise.", ["significant"]),
        ("Significantly more complaints.", ["significantly"]),
        ("Statistically, this is MEANINGFUL.", ["statistically", "meaningful"]),
        ("This is not random.", ["not random"]),
        ("More than random variation.", ["random variation"]),
        ("A non-significant gap.", ["significant"]),
        ("significant and significant again", ["significant"]),
    ])
    def test_whole_words_any_case(self, text, found):
        assert banned_wording(text) == found

    @pytest.mark.parametrize("text", ["An insignificant gap.", "It matters meaningfully.",
                                      "Not randomly chosen.", "No significance claimed.",
                                      "A random sample of variation."])
    def test_longer_or_other_words_are_accepted(self, text):
        assert banned_wording(text) == []

    def test_the_word_list(self):
        assert BANNED_WORDS == ("significant", "significantly", "statistically",
                                "meaningful", "not random", "random variation")

    @pytest.mark.parametrize("field", ["title", "description", "rationale", "evidence"])
    def test_reply_using_them_fails_and_names_them(self, field):
        reply = good_reply()
        reply["items"][0][field] = "Review a significant, not\nrandom stand fault."
        with pytest.raises(ActionItemsValidationError,
                           match=f"item 1 {field} uses wording the summary cannot support: "
                                 "significant, not random"):
            validate_action_items(reply, AREAS)

    def test_related_area_is_not_checked(self):
        reply = good_reply()
        reply["items"][0]["related_area"] = "Meaningful Group"
        validate_action_items(reply, summary_for(sector_groups=(("Meaningful Group", False),)))


class TestFieldIdentifiers:
    @pytest.mark.parametrize("text, found", [
        ("Marked small_sample: true.", ["small_sample"]),
        ("few_mentions and negative_share_pct", ["few_mentions", "negative_share_pct"]),
        ("(review_source) and review_source again", ["review_source"]),
        ("share_of_reviews_pct was 12.5%", ["share_of_reviews_pct"]),
        ("top_3_themes", ["top_3_themes"]),
    ])
    def test_snake_case_words_are_found(self, text, found):
        assert field_identifiers(text) == found

    @pytest.mark.parametrize("text", [
        "A small sample: too few reviews to tell.",
        "40 of 120 reviews (33.3%) were negative in 2026-09.",
        "Check the well-known E27 bulb from 1 March 2026.",
        "Product D35500 had 1,200 reviews, 12 per cent negative.",
        "A Mixed_Case or ALL_CAPS name, and _leading or trailing_ underscores.",
    ])
    def test_ordinary_text_is_accepted(self, text):
        assert field_identifiers(text) == []

    @pytest.mark.parametrize("field", ["title", "description", "rationale", "evidence"])
    def test_reply_using_them_fails_and_names_them(self, field):
        reply = good_reply()
        reply["items"][0][field] = "Review the stand: small_sample and few_mentions."
        with pytest.raises(ActionItemsValidationError,
                           match=f"item 1 {field} copies field names from the data: "
                                 "small_sample, few_mentions; write them in plain words "
                                 'such as "small sample" or "too few reviews"'):
            validate_action_items(reply, AREAS)

    def test_plain_reply_is_accepted(self):
        reply = good_reply()
        reply["items"][0]["rationale"] = ("A small sample, so there are too few reviews "
                                          "to tell; a well-known, low-cost fix.")
        validate_action_items(reply, AREAS)


def few_mentions_rows(earlier_negatives, recent_negatives):
    """One sector group with 50 earlier and 20 recent reviews, the given
    numbers of them negative and mentioning "flicker"."""
    rows, i = [], 0
    for day, reviews, negatives in [(date(2026, 1, 10), 50, earlier_negatives),
                                    (date(2026, 9, 1), 20, recent_negatives)]:
        for k in range(reviews):
            rows.append(review(i, day, "negative" if k < negatives else "positive",
                               sector_group="Sewing & Needlecraft",
                               key_themes='["flicker"]'))
            i += 1
    return rows


class TestFewMentions:
    @pytest.mark.parametrize("earlier, recent, few", [(6, 3, True), (7, 3, False),
                                                     (0, 9, True), (0, 10, False)])
    def test_flag_at_the_threshold(self, earlier, recent, few):
        assert MIN_TREND_COUNT == 10
        trend = build_summary(year_window(few_mentions_rows(earlier, recent)),
                              15, 10, 300)["trend"]
        [sewing] = trend["product_by_sector_group"]
        assert sewing["few_mentions"] is few
        [flicker] = trend["top_negative_themes"]
        assert flicker["few_mentions"] is few
        for section in ("by_review_type_and_source", "product_by_region_and_source"):
            assert [row["few_mentions"] for row in trend[section]] == [few]

    def test_threshold_can_be_passed_in(self):
        window = year_window(few_mentions_rows(3, 2))
        summary = build_summary(window, 15, 10, 300)
        assert build_trend(window, summary, 30, min_trend_count=5)[
            "product_by_sector_group"][0]["few_mentions"] is False
        assert build_trend(window, summary, 30, min_trend_count=6)[
            "top_negative_themes"][0]["few_mentions"] is True

    def test_flag_adds_no_numbers_for_the_validator(self):
        summary = build_summary(year_window(trend_rows()), 15, 10, 300)

        def without_flag(value):
            if isinstance(value, dict):
                return {k: without_flag(v) for k, v in value.items() if k != "few_mentions"}
            if isinstance(value, list):
                return [without_flag(v) for v in value]
            return value

        assert '"few_mentions"' in json.dumps(summary)
        assert summary_numbers(summary) == summary_numbers(without_flag(summary))


class TestReliabilityPromptRules:
    def prompt(self):
        summary = build_summary(year_window(trend_rows()), 15, 10, 300)
        return build_action_items_prompt(summary, summary_areas(summary))

    def test_rules(self):
        raw = self.prompt()
        flat = " ".join(raw.split())
        for rule in (
                'A row is marked "few_mentions": true when its negative reviews (for a '
                "topic, the negative reviews mentioning it) add up to fewer than 10 across "
                "both parts: too few to tell whether it is rising or falling.",
                'An "early signal" item\'s title follows rule 13.',
                'Do not use these words anywhere in an item, in any sense: "significant", '
                '"significantly", "statistically", "meaningful", "not random" or "random '
                'variation".',
                '"moderate" and "strong" also require that the key figures do not rest on '
                'rows marked "small_sample": true or "few_mentions": true.',
                'A topic or product with fewer than 10 negative reviews is an "early signal".',
                'The title of an "early signal" item must start with one of "Monitor", '
                '"Investigate" or "Review", never with one of "Fix", "Overhaul", "Resolve", '
                '"Eliminate", "Stop" or "Eradicate".',
                'For a row marked "small_sample": true or "few_mentions": true, never say '
                "that it is rising, worsening, improving, increased or decreased; say that "
                "there are too few reviews to tell.",
                "16. This period alone. Write the items for this period only. Other periods "
                "get their own items, so do not repeat an issue only because it appears in "
                "another period.",
                "17. Never copy field names, flags or true/false values from the data into "
                'an item. Say "small sample" or "too few reviews to tell" in plain words.'):
            assert rule in flat, rule
        assert '"few_mentions": false' in raw


class TestRetryOnTheNewChecks:
    def test_failing_checks_trigger_the_single_retry_with_the_errors(self):
        bad = dataset_reply()
        bad["items"][1]["title"] = "Fix delivery packaging"  # rank 1, early signal
        bad["items"][0]["rationale"] = "A significant 40 of 120 product reviews."
        analyzer = FakeAnalyzer(replies=[bad, dataset_reply()])
        repo = FakeRepository(dataset())
        outcome = TestGenerate().generate(analyzer, repo)
        assert len(analyzer.calls) == 2 and len(repo.saved) == 1
        assert not outcome.failed
        retry = analyzer.calls[1][0]
        assert ("item 1 rationale uses wording the summary cannot support: significant"
                in retry)
        assert ("item 2 is an 'early signal' but its title starts with 'Fix'; start it with "
                "Monitor, Investigate or Review") in retry

    def test_field_name_triggers_the_single_retry_with_the_error(self):
        bad = dataset_reply()
        bad["items"][0]["evidence"] = "This group is marked small_sample: true."
        analyzer = FakeAnalyzer(replies=[bad, dataset_reply()])
        repo = FakeRepository(dataset())
        outcome = TestGenerate().generate(analyzer, repo)
        assert len(analyzer.calls) == 2 and len(repo.saved) == 1
        assert not outcome.failed
        assert ("item 1 evidence copies field names from the data: small_sample; write "
                'them in plain words such as "small sample" or "too few reviews"'
                in analyzer.calls[1][0])
        assert "small_sample" not in json.dumps(repo.saved, default=str)

    def test_failing_twice_saves_nothing(self):
        bad = dataset_reply()
        bad["items"][1]["title"] = "Stop the damage"
        analyzer = FakeAnalyzer(replies=[bad, bad])
        repo = FakeRepository(dataset())
        outcome = TestGenerate().generate(analyzer, repo)
        [failed] = outcome.failed
        assert "title starts with 'Stop'" in str(failed.error)
        assert len(analyzer.calls) == 2 and repo.saved == []
