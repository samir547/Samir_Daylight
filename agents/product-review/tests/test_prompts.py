"""Characterization: prompt-building helpers (_build_context, _render_batch,
_build_batch_prompt)."""
from daylight_sentiment.domain.prompts import (
    _build_batch_prompt,
    _build_context,
    _render_batch,
)


class TestBuildContext:
    def test_empty_when_nothing_given(self):
        assert _build_context(None, None) == ""

    def test_product_only(self):
        assert _build_context("TriSun Lamp", None) == "Product: TriSun Lamp\n"

    def test_rating_only(self):
        assert _build_context(None, 4) == "Star rating: 4/5\n"

    def test_both(self):
        assert _build_context("TriSun Lamp", 4) == "Product: TriSun Lamp\nStar rating: 4/5\n"

    def test_rating_zero_still_shown(self):
        assert _build_context(None, 0) == "Star rating: 0/5\n"


class TestRenderBatch:
    def test_product_review_gets_product_and_rating_lines(self):
        block = _render_batch([{"review_type": "product", "product_name": "Lamp A",
                                "rating": 5, "text": "Great."}], max_chars=6000)
        assert block == "--- Review 1 ---\nProduct: Lamp A\nStar rating: 5/5\nGreat."

    def test_service_review_never_gets_product_line(self):
        block = _render_batch([{"review_type": "service", "product_name": "Lamp A",
                                "rating": 5, "text": "Fast delivery."}], max_chars=6000)
        assert "Product:" not in block
        assert "Star rating: 5/5" in block

    def test_non_int_rating_skipped(self):
        block = _render_batch([{"review_type": "product", "text": "ok", "rating": None}],
                              max_chars=6000)
        assert "Star rating" not in block

    def test_text_truncated_to_max_chars(self):
        block = _render_batch([{"review_type": "service", "text": "x" * 100}], max_chars=10)
        assert block == "--- Review 1 ---\n" + "x" * 10

    def test_numbering_and_separation(self):
        block = _render_batch([{"review_type": "service", "text": "a"},
                               {"review_type": "service", "text": "b"}], max_chars=6000)
        assert "--- Review 1 ---\na\n\n--- Review 2 ---\nb" == block


class TestBuildBatchPrompt:
    def test_product_template_selected(self):
        prompt = _build_batch_prompt(
            [{"review_type": "product", "text": "nice"}], "product", 6000)
        assert "primary_sector" in prompt
        assert "primary_aspect" not in prompt
        assert "Return exactly 1 objects" in prompt

    def test_service_template_selected(self):
        prompt = _build_batch_prompt(
            [{"review_type": "service", "text": "nice"}, {"review_type": "service", "text": "ok"}],
            "service", 6000)
        assert "primary_aspect" in prompt
        assert "primary_sector" not in prompt
        assert "Return exactly 2 objects" in prompt

    def test_sentiment_guidance_present_in_both(self):
        for review_type in ("product", "service"):
            prompt = _build_batch_prompt([{"text": "x"}], review_type, 6000)
            assert "OVERALL experience" in prompt
