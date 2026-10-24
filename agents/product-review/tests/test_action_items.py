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
    ActionItemsValidationError,
    FIELD_MAX_CHARS,
    SMALL_SAMPLE_REVIEWS,
    ReviewWindow,
    aspect_name,
    build_summary,
    count_negative_themes,
    format_action_items,
    months_before,
    theme_key,
    parse_themes,
    products_most_negative,
    select_excerpts,
    select_window,
    small_sample_areas,
    summary_areas,
    summary_numbers,
    unsupported_numbers,
    validate_action_items,
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


def window_of(rows):
    dates = [r["review_date_parsed"] for r in rows]
    return ReviewWindow(rows, min(dates), max(dates))


class TestSelectWindow:
    def test_window_end_is_newest_review_date_not_today(self):
        rows = [review(i, date(2024, 3, 1 + i % 28)) for i in range(120)]
        w = select_window(rows, 12, 100, 50)
        assert w.end == date(2024, 3, 28)
        assert w.start == date(2023, 3, 29)
        assert not w.widened and w.review_count == 120

    def test_filters_to_last_n_months(self):
        recent = [review(i, date(2026, 8, 20)) for i in range(100)]
        old = [review(1000 + i, date(2025, 8, 20)) for i in range(30)]
        boundary = [review(2000, date(2025, 8, 21))]
        w = select_window(recent + old + boundary, 12, 100, 50)
        assert w.start == date(2025, 8, 21) and w.end == date(2026, 8, 20)
        assert w.review_count == 101
        assert all(r["review_date_parsed"] >= w.start for r in w.rows)

    def test_months_setting_changes_the_window(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(100)]
                + [review(1000 + i, date(2026, 3, 1)) for i in range(100)])
        assert select_window(rows, 3, 100, 50).review_count == 100
        assert select_window(rows, 12, 100, 50).review_count == 200

    def test_widens_to_all_dated_reviews_when_window_is_thin(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(40)]
                + [review(1000 + i, date(2020, 1, 1)) for i in range(40)])
        w = select_window(rows, 12, 100, 50)
        assert w.widened and not w.skipped
        assert w.review_count == 80
        assert w.start == date(2020, 1, 1) and w.end == date(2026, 8, 1)

    def test_skips_when_under_minimum_in_total(self):
        rows = [review(i, date(2026, 8, 1)) for i in range(49)]
        w = select_window(rows, 12, 100, 50)
        assert w.skipped and "49" in w.skip_reason

    def test_undated_and_unanalysed_rows_are_excluded_and_counted(self):
        rows = ([review(i, date(2026, 8, 1)) for i in range(100)]
                + [review(500 + i, None) for i in range(7)]
                + [review(900, "not a date")]
                + [review(950, date(2026, 8, 1), sentiment=None)])
        w = select_window(rows, 12, 100, 50)
        assert w.review_count == 100
        assert w.undated_excluded == 8

    def test_no_dated_reviews_skips(self):
        w = select_window([review(1, None)], 12, 100, 50)
        assert w.skipped and w.undated_excluded == 1

    def test_accepts_datetimes_and_iso_strings(self):
        rows = ([review(i, datetime(2026, 8, 1, 12)) for i in range(60)]
                + [review(100 + i, "2026-07-01") for i in range(60)])
        w = select_window(rows, 12, 100, 50)
        assert w.review_count == 120 and w.end == date(2026, 8, 1)

    def test_future_date_does_not_move_the_window_past_today(self):
        rows = ([review(i, date(2026, 8, 20)) for i in range(120)]
                + [review(500, date(2027, 5, 1))])
        w = select_window(rows, 12, 100, 50, today=date(2026, 10, 24))
        assert w.end == date(2026, 10, 24) and w.start == date(2025, 10, 25)
        assert w.capped_from == date(2027, 5, 1) and w.future_dated == 1
        assert w.review_count == 121  # the future-dated review is kept

    def test_no_cap_when_the_newest_date_is_not_in_the_future(self):
        rows = [review(i, date(2026, 10, 24)) for i in range(120)]
        w = select_window(rows, 12, 100, 50, today=date(2026, 10, 24))
        assert w.end == date(2026, 10, 24)
        assert w.capped_from is None and w.future_dated == 0

    def test_cap_applies_to_a_widened_window(self):
        rows = ([review(i, date(2020, 1, 1)) for i in range(60)]
                + [review(500, date(2030, 1, 1))])
        w = select_window(rows, 12, 100, 50, today=date(2026, 10, 24))
        assert w.widened and w.end == date(2026, 10, 24) and w.start == date(2020, 1, 1)
        assert w.capped_from == date(2030, 1, 1)

    def test_defaults_to_the_current_date(self):
        future = date.today() + timedelta(days=400)
        rows = [review(i, future) for i in range(120)]
        w = select_window(rows, 12, 100, 50)
        assert w.end == date.today() and w.start <= w.end

    def test_months_before_clamps_short_months(self):
        assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
        assert months_before(date(2026, 1, 15), 12) == date(2025, 1, 15)


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
        assert s["window"] == {"start": "2026-05-02", "end": "2026-06-15",
                               "reviews": 6, "widened_to_all_dates": False}
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
            {"product": "Lumi Task Lamp", "sku": "D35500", "region": "UK", "reviews": 40,
             "negatives": 6, "negative_share_pct": 15.0, "small_sample": False},
            {"product": "Halo Floor Lamp", "sku": "U31000", "region": "US", "reviews": 12,
             "negatives": 3, "negative_share_pct": 25.0, "small_sample": True},
        ]

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
        assert len(ex["text"]) == 300 and ex["text"].endswith("...")

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
                'Use "General / No Use Stated" only for an issue about that group itself'):
            assert rule in flat, rule

    def test_summary_areas(self):
        summary = build_summary(window_of(TestBuildSummary().rows()), 15, 10, 300)
        assert summary_areas(summary) == [
            "overall", "Sewing & Needlecraft", "Art & Painting",
            "Shipping quality", "Customer service", "UK", "EU"]


def test_format_action_items():
    rows = [review(i, date(2026, 6, 1)) for i in range(3)]
    text = format_action_items(validate_action_items(good_reply(), AREAS), window_of(rows))
    assert text.startswith("Top 3 to-do items -- Based on 3 reviews from 2026-06-01 to 2026-06-01")
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

    def save(self, items, window_start, window_end, reviews_analysed, llm_provider,
             llm_model, run_timestamp=None):
        self.saved.append(dict(items=items, window_start=window_start, window_end=window_end,
                               reviews_analysed=reviews_analysed, llm_provider=llm_provider,
                               llm_model=llm_model, run_timestamp=run_timestamp))
        return "generation-1"


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
    def run(self, reply=None, error=None, rows=None, dry_run=False):
        analyzer = FakeAnalyzer(reply if reply is not None else dataset_reply(), error)
        repo = FakeRepository(rows if rows is not None else dataset())
        outcome = generate_action_items(analyzer, repo, ActionItemSettings(), "claude",
                                        "model-x", run_timestamp="2026-10-24T10:00:00",
                                        dry_run=dry_run)
        return outcome, analyzer, repo

    def test_one_call_and_saved_with_window(self):
        outcome, analyzer, repo = self.run()
        assert len(analyzer.calls) == 1  # valid first time: no retry
        assert analyzer.calls[0][1] == ACTION_ITEMS_MAX_TOKENS
        assert outcome.generation_id == "generation-1"
        [saved] = repo.saved
        assert [i["rank"] for i in saved["items"]] == [1, 2, 3]
        assert saved["window_start"] == date(2025, 8, 21)
        assert saved["window_end"] == date(2026, 8, 20)
        assert saved["reviews_analysed"] == 121
        assert (saved["llm_provider"], saved["llm_model"]) == ("claude", "model-x")
        assert saved["run_timestamp"] == "2026-10-24T10:00:00"

    def test_dry_run_saves_nothing(self):
        outcome, analyzer, repo = self.run(dry_run=True)
        assert len(outcome.items) == 3 and outcome.generation_id is None
        assert repo.saved == []

    def test_invalid_reply_saves_nothing(self):
        reply = good_reply()
        reply["items"].pop()
        with pytest.raises(ActionItemsValidationError):
            self.run(reply=reply)

    def generate(self, analyzer, repo, dry_run=False):
        return generate_action_items(analyzer, repo, ActionItemSettings(), "claude", "m",
                                     dry_run=dry_run)

    def test_invalid_then_valid_reply_is_saved_once(self, caplog):
        bad = dataset_reply()
        bad["items"][0]["rationale"] = "47.3% of buyers complain."
        analyzer = FakeAnalyzer(replies=[bad, dataset_reply()])
        repo = FakeRepository(dataset())
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome = self.generate(analyzer, repo)
        assert len(analyzer.calls) == 2 and len(repo.saved) == 1
        assert [i["rank"] for i in outcome.items] == [1, 2, 3]
        first, retry = analyzer.calls[0][0], analyzer.calls[1][0]
        assert retry.startswith(first)
        assert ("Your previous reply failed because: item 1 rationale has numbers that are "
                "not in the summary: 47.3%") in retry
        assert analyzer.calls[1][1] == ACTION_ITEMS_MAX_TOKENS
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "retrying once" in warnings[0].getMessage()
        assert "47.3%" in warnings[0].getMessage()
        assert warnings[0].exc_info is None

    def test_both_replies_invalid_saves_nothing(self, caplog):
        analyzer = FakeAnalyzer(replies=[{"items": []}, {"items": [1, 2, 3]}])
        repo = FakeRepository(dataset())
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            with pytest.raises(ActionItemsValidationError, match="item 1 is not an object"):
                self.generate(analyzer, repo)
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
        with pytest.raises(ActionItemsValidationError):
            self.generate(analyzer, repo, dry_run=True)
        assert repo.saved == []

    def test_provider_error_is_not_retried(self):
        analyzer = FakeAnalyzer(error=FatalProviderError("bad key"))
        with pytest.raises(FatalProviderError):
            self.generate(analyzer, FakeRepository(dataset()))
        assert len(analyzer.calls) == 1

    def test_retry_prompt(self):
        assert build_retry_prompt("PROMPT", "x; y") == (
            "PROMPT\n\nYour previous reply failed because: x; y\n\nWrite the 3 items "
            "again, following every rule above, and respond ONLY with\nJSON in the same "
            "shape.")

    def test_invalid_reply_repository_untouched(self):
        repo = FakeRepository(dataset())
        with pytest.raises(ActionItemsValidationError):
            generate_action_items(FakeAnalyzer({"items": []}), repo, ActionItemSettings(),
                                  "claude", "m")
        assert repo.saved == []

    def test_thin_data_skips_without_calling_the_model(self, caplog):
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome, analyzer, repo = self.run(rows=dataset(10))
        assert outcome.skipped and outcome.items == []
        assert analyzer.calls == [] and repo.saved == []
        assert "not generated" in caplog.text

    def test_window_cap_is_logged(self, caplog):
        rows = dataset() + [review(9999, date.today() + timedelta(days=30), review_type="service")]
        with caplog.at_level("WARNING", logger="multi_source_sentiment"):
            outcome, _, repo = self.run(rows=rows)
        assert repo.saved[0]["window_end"] == date.today()
        assert outcome.window.capped_from == date.today() + timedelta(days=30)
        assert "is after today, so the window ends today" in caplog.text
        assert "1 review(s) dated after today" in caplog.text

    def test_provider_error_propagates(self):
        with pytest.raises(FatalProviderError):
            self.run(error=FatalProviderError("bad key"))

    def test_prompt_is_built_from_the_summary_only(self):
        rows = dataset()
        for r in rows:
            r["reviewer_name"] = "Private Person"
        _, analyzer, _ = self.run(rows=rows)
        prompt = analyzer.calls[0][0]
        assert "Private Person" not in prompt
        assert '"reviews": 121' in prompt
