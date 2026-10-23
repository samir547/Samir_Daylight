"""Business to-do items: window selection, the summary the model is shown,
the prompt, validation of the reply, and the orchestration with a fake
analyzer and a fake repository (no API or database access)."""
import json
from datetime import date, datetime

import pytest

from daylight_sentiment.application.action_items import (
    ACTION_ITEMS_MAX_TOKENS,
    generate_action_items,
)
from daylight_sentiment.config import ActionItemSettings
from daylight_sentiment.domain.action_items import (
    ActionItemsValidationError,
    FIELD_MAX_CHARS,
    ReviewWindow,
    build_summary,
    format_action_items,
    months_before,
    parse_themes,
    select_excerpts,
    select_window,
    summary_areas,
    validate_action_items,
)
from daylight_sentiment.domain.action_items_prompt import build_action_items_prompt
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
            "name": "Sewing & Needlecraft", "reviews": 3, "negative": 2,
            "negative_share_pct": 66.7}
        assert sectors["Art & Painting"]["negative"] == 0
        aspects = {e["name"]: e for e in s["service_by_aspect"]}
        assert aspects["shipping_quality"]["negative_share_pct"] == 100.0
        assert {e["name"] for e in s["product_by_region"]} == {"UK", "EU"}
        assert s["by_review_type_and_source"] == [
            {"review_type": "product", "review_source": "trustpilot",
             "reviews": 4, "negative": 2, "negative_share_pct": 50.0},
            {"review_type": "service", "review_source": "trustpilot",
             "reviews": 2, "negative": 1, "negative_share_pct": 50.0},
        ]

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
            {"month": "2026-05", "reviews": 2, "negative": 0, "negative_share_pct": 0.0},
            {"month": "2026-06", "reviews": 4, "negative": 3, "negative_share_pct": 75.0},
        ]

    def test_no_region_section_without_regions(self):
        rows = [review(i, date(2026, 6, 1), product_region=None) for i in range(3)]
        assert "product_by_region" not in build_summary(window_of(rows), 15, 10, 300)

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
            "Sewing & Needlecraft", "shipping_quality", "Beauty"]
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
         "evidence": "12 negative reviews mention the stand."},
        {"rank": 1, "title": "Fix delivery packaging", "description": "Do it.",
         "rationale": "9 of 10.", "related_area": "SHIPPING_QUALITY",
         "evidence": "9 service reviews."},
        {"rank": 3, "title": "Check flicker reports", "description": "Do it.",
         "rationale": "Only 4 reviews, so this is tentative.", "related_area": "overall",
         "evidence": "4 negative reviews mention flicker."},
    ]}


AREAS = ["overall", "Sewing & Needlecraft", "shipping_quality"]


class TestValidate:
    def test_accepts_good_reply_sorted_and_canonicalised(self):
        items = validate_action_items(good_reply(), AREAS)
        assert [i["rank"] for i in items] == [1, 2, 3]
        assert items[0]["related_area"] == "shipping_quality"

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
                                      "related_area", "evidence"])
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
            validate_action_items(reply, AREAS + ["x" * (FIELD_MAX_CHARS[name] + 1)])

    def test_db_column_sizes_bound_the_caps(self):
        assert FIELD_MAX_CHARS["title"] <= 200
        assert FIELD_MAX_CHARS["related_area"] <= 100

    @pytest.mark.parametrize("reply", [None, [], {}, {"items": "x"}, {"items": [1, 2, 3]}])
    def test_rejects_malformed_reply(self, reply):
        with pytest.raises(ActionItemsValidationError):
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
                     '"rationale"', '"related_area"', '"evidence"'):
            assert rule in prompt, rule

    def test_summary_areas(self):
        summary = build_summary(window_of(TestBuildSummary().rows()), 15, 10, 300)
        assert summary_areas(summary) == [
            "overall", "Sewing & Needlecraft", "Art & Painting",
            "shipping_quality", "customer_service", "UK", "EU"]


def test_format_action_items():
    rows = [review(i, date(2026, 6, 1)) for i in range(3)]
    text = format_action_items(validate_action_items(good_reply(), AREAS), window_of(rows))
    assert text.startswith("Top 3 to-do items -- Based on 3 reviews from 2026-06-01 to 2026-06-01")
    assert "1. Fix delivery packaging  [shipping_quality]" in text


# -- orchestration ------------------------------------------------------------

class FakeAnalyzer(SentimentAnalyzer):
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error
        self.calls = []

    def complete_json(self, prompt, max_tokens=None):
        self.calls.append((prompt, max_tokens))
        if self.error:
            raise self.error
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


class TestGenerate:
    def run(self, reply=None, error=None, rows=None, dry_run=False):
        analyzer = FakeAnalyzer(reply if reply is not None else good_reply(), error)
        repo = FakeRepository(rows if rows is not None else dataset())
        outcome = generate_action_items(analyzer, repo, ActionItemSettings(), "claude",
                                        "model-x", run_timestamp="2026-10-24T10:00:00",
                                        dry_run=dry_run)
        return outcome, analyzer, repo

    def test_one_call_and_saved_with_window(self):
        outcome, analyzer, repo = self.run()
        assert len(analyzer.calls) == 1
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
