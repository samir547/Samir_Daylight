"""Characterization: _align_batch -- mapping a batched model response back onto
input order via echoed ids, with positional fill for leftovers."""
import pytest

from daylight_sentiment.domain.normalise import _align_batch

REVIEWS = [{"uid": f"u{i}", "text": f"t{i}"} for i in range(3)]


def _item(i, sentiment="positive"):
    return {"id": i, "sentiment": sentiment, "primary_sector": "beauty",
            "confidence": 0.9, "key_themes": []}


class TestAlignBatch:
    def test_ids_map_to_slots(self):
        raw = {"results": [_item(2, "negative"), _item(1, "positive"), _item(3, "neutral")]}
        out = _align_batch(raw, REVIEWS, "product")
        assert [o["sentiment"] for o in out] == ["positive", "negative", "neutral"]

    def test_dropped_review_leaves_none(self):
        raw = {"results": [_item(1), _item(3)]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out[0] is not None
        assert out[1] is None
        assert out[2] is not None

    def test_missing_results_key_gives_all_none(self):
        assert _align_batch({"answers": []}, REVIEWS, "product") == [None, None, None]

    def test_results_not_a_list_gives_all_none(self):
        assert _align_batch({"results": "nope"}, REVIEWS, "product") == [None, None, None]

    def test_duplicate_id_fills_next_empty_slot_positionally(self):
        raw = {"results": [_item(1, "positive"), _item(1, "negative")]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out[0]["sentiment"] == "positive"
        assert out[1]["sentiment"] == "negative"  # leftover filled positionally
        assert out[2] is None

    def test_out_of_range_id_treated_as_leftover(self):
        raw = {"results": [_item(99, "negative")]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out[0]["sentiment"] == "negative"

    def test_unparseable_id_treated_as_leftover(self):
        raw = {"results": [{"id": "one", "sentiment": "negative",
                            "primary_sector": "beauty", "confidence": 0.5,
                            "key_themes": []}]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out[0]["sentiment"] == "negative"

    def test_non_dict_items_skipped(self):
        raw = {"results": ["garbage", _item(2)]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out == [None, out[1], None]
        assert out[1]["sentiment"] == "positive"

    def test_results_are_normalised(self):
        raw = {"results": [{"id": 1, "sentiment": "MIXED", "primary_sector": "nope!",
                            "confidence": 5, "key_themes": "one"}]}
        out = _align_batch(raw, REVIEWS, "product")
        assert out[0] == {"sentiment": "neutral", "confidence": 1.0,
                          "key_themes": ["one"], "primary_sector": "nope"}

    def test_top_level_list_currently_crashes(self):
        # CHARACTERIZATION of a latent bug: _align_batch has an
        # `isinstance(raw, list)` branch, but `raw.get("results")` runs first,
        # so a model that returns a top-level JSON array raises AttributeError
        # here. In practice the callers' broad `except Exception` turns this
        # into a full per-review retry, so it's wasteful rather than fatal.
        # Pinned as-is; flagged in REFACTOR_PROGRESS.md rather than fixed
        # silently mid-refactor.
        with pytest.raises(AttributeError):
            _align_batch([_item(1)], REVIEWS, "product")
