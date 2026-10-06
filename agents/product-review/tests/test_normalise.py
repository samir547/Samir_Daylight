"""Characterization: _normalise, _sanitise_sector_label, _strip_fences."""
from daylight_sentiment.domain.normalise import (
    _normalise,
    _sanitise_sector_label,
    _strip_fences,
)
from daylight_sentiment.domain.taxonomy import ASPECTS, SECTORS, SENTIMENTS


class TestSanitiseSectorLabel:
    def test_basic_slugify(self):
        assert _sanitise_sector_label(" Wood-Working ") == "wood_working"
        assert _sanitise_sector_label("3D printing!") == "3d_printing"

    def test_collapses_and_trims_underscores(self):
        assert _sanitise_sector_label("__a  -  b__") == "a_b"

    def test_strips_non_ascii_alnum(self):
        assert _sanitise_sector_label("café & décor") == "caf_dcor"

    def test_caps_at_40_chars(self):
        assert len(_sanitise_sector_label("x" * 100)) == 40

    def test_unusable_becomes_empty(self):
        assert _sanitise_sector_label("!!!") == ""


class TestNormaliseProduct:
    def test_happy_path(self):
        out = _normalise({"sentiment": "Positive", "primary_sector": "beauty",
                          "confidence": 0.9, "key_themes": ["Bright Light", "  "]},
                         "product")
        assert out == {"sentiment": "positive", "confidence": 0.9,
                       "key_themes": ["bright light"], "primary_sector": "beauty"}

    def test_invalid_sentiment_falls_back_to_neutral(self):
        assert _normalise({"sentiment": "mixed"}, "product")["sentiment"] == "neutral"
        assert _normalise({}, "product")["sentiment"] == "neutral"

    def test_confidence_clamped_and_defaulted(self):
        assert _normalise({"confidence": 1.7}, "product")["confidence"] == 1.0
        assert _normalise({"confidence": -2}, "product")["confidence"] == 0.0
        assert _normalise({"confidence": "high"}, "product")["confidence"] == 0.5
        assert _normalise({}, "product")["confidence"] == 0.5

    def test_string_theme_wrapped_in_list(self):
        assert _normalise({"key_themes": "durability"}, "product")["key_themes"] == ["durability"]

    def test_themes_capped_at_six(self):
        out = _normalise({"key_themes": [f"t{i}" for i in range(10)]}, "product")
        assert out["key_themes"] == ["t0", "t1", "t2", "t3", "t4", "t5"]

    def test_seeded_sector_kept(self):
        for sector in SECTORS:
            assert _normalise({"primary_sector": sector}, "product")["primary_sector"] == sector

    def test_novel_sector_sanitised_not_collapsed_to_general(self):
        # SECTORS is a seed list, not a closed set -- new labels survive.
        out = _normalise({"primary_sector": "Wood Working"}, "product")
        assert out["primary_sector"] == "wood_working"

    def test_empty_or_unusable_sector_falls_back_to_general(self):
        assert _normalise({"primary_sector": ""}, "product")["primary_sector"] == "general"
        assert _normalise({"primary_sector": "!!!"}, "product")["primary_sector"] == "general"
        assert _normalise({}, "product")["primary_sector"] == "general"


class TestNormaliseService:
    def test_aspect_is_a_closed_set(self):
        # Unlike sectors, a novel aspect is coerced to the catch-all.
        out = _normalise({"primary_aspect": "unboxing_experience"}, "service")
        assert out["primary_aspect"] == "general_experience"

    def test_known_aspect_kept(self):
        for aspect in ASPECTS:
            assert _normalise({"primary_aspect": aspect}, "service")["primary_aspect"] == aspect

    def test_no_sector_key_on_service(self):
        out = _normalise({"primary_aspect": "warranty"}, "service")
        assert "primary_sector" not in out


class TestStripFences:
    def test_plain_json_untouched(self):
        assert _strip_fences('{"a": 1}') == '{"a": 1}'

    def test_fenced_with_language_tag(self):
        assert _strip_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_fenced_without_newline(self):
        assert _strip_fences('```{"a": 1}```') == '{"a": 1}'

    def test_opening_fence_only(self):
        assert _strip_fences('```json\n{"a": 1}') == '{"a": 1}'

    def test_vocabularies_unchanged(self):
        # Pin the classification vocabularies themselves -- a refactor must
        # not silently alter what the DB/Power BI receive.
        assert SENTIMENTS == ["positive", "negative", "neutral"]
        assert SECTORS == ["sewing_needlecraft", "art_painting",
                           "reading_vision_wellbeing", "beauty", "medical",
                           "jewellery_trade", "general"]
        assert ASPECTS == ["shipping_quality", "customer_service", "product_quality",
                           "fulfillment_speed", "communication", "returns_refunds",
                           "warranty", "general_experience"]
