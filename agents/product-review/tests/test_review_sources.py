"""ISSUE-01/02: FilePattern matching, local file resolution, SharePoint
pattern selection (pure, no network), source loaders (synthetic files), and
load_reviews() uid assignment."""
import time

import pandas as pd
import pytest

from daylight_sentiment.config import FilePattern, SourceFiles
from daylight_sentiment.infra.review_sources import (
    AmazonBrightDataSource,
    TrustpilotProductSource,
    TrustpilotServiceSource,
    build_default_sources,
    load_reviews,
)
from daylight_sentiment.infra.review_sources.base import resolve_source_file
from daylight_sentiment.infra.sharepoint_sync import select_items_by_patterns

TP_PRODUCT = FilePattern(("trustpilot", "product"), ".xlsx")


class TestFilePattern:
    def test_matches_current_production_names(self):
        src = SourceFiles()
        assert src.trustpilot_product_pattern.matches("trustpilot-product-reviews 873.xlsx")
        assert src.trustpilot_service_pattern.matches("TrustPilot Service Reviews.xlsx")
        assert src.amazon_product_pattern.matches("amazon_product_reviews_all.csv")

    def test_matches_renamed_exports(self):
        # The exact failure mode ISSUE-01 describes: fresh row count, "(2)",
        # date stamps.
        assert TP_PRODUCT.matches("trustpilot-product-reviews 901.xlsx")
        assert TP_PRODUCT.matches("Trustpilot-Product-Reviews (2).xlsx")
        assert TP_PRODUCT.matches("2026-09-17 trustpilot product export.xlsx")

    def test_rejects_wrong_source_or_extension(self):
        assert not TP_PRODUCT.matches("TrustPilot Service Reviews.xlsx")
        assert not TP_PRODUCT.matches("trustpilot-product-reviews.csv")
        assert not SourceFiles().trustpilot_service_pattern.matches(
            "trustpilot-product-reviews 873.xlsx")


class TestResolveSourceFile:
    def test_picks_newest_pattern_match(self, tmp_path):
        wrong_ext = tmp_path / "trustpilot-product-reviews 873.xlsx.old"  # wrong ext, ignored
        wrong_ext.write_bytes(b"")
        older = tmp_path / "trustpilot-product-reviews 880.xlsx"
        older.write_bytes(b"x")
        newest = tmp_path / "trustpilot-product-reviews 901 (2).xlsx"
        newest.write_bytes(b"y")
        now = time.time()
        import os
        os.utime(older, (now - 100, now - 100))
        os.utime(newest, (now, now))
        assert resolve_source_file(TP_PRODUCT, tmp_path) == newest

    def test_no_match_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No file in .* matches"):
            resolve_source_file(TP_PRODUCT, tmp_path)


class TestSharePointSelection:
    def _item(self, name, modified="2026-09-01T00:00:00Z"):
        return {"name": name, "lastModifiedDateTime": modified}

    def test_picks_newest_match_per_pattern(self):
        items = [self._item("trustpilot-product-reviews 873.xlsx", "2026-09-01T00:00:00Z"),
                 self._item("trustpilot-product-reviews 901.xlsx", "2026-09-15T00:00:00Z"),
                 self._item("TrustPilot Service Reviews.xlsx")]
        chosen = select_items_by_patterns(
            items, [TP_PRODUCT, FilePattern(("trustpilot", "service"), ".xlsx")])
        assert [c["name"] for c in chosen] == [
            "trustpilot-product-reviews 901.xlsx", "TrustPilot Service Reviews.xlsx"]

    def test_rename_is_tolerated(self):
        # Any name matching the pattern is picked -- nothing is ever
        # compared against a specific expected filename.
        items = [self._item("trustpilot-product-reviews 901 (2).xlsx")]
        chosen = select_items_by_patterns(items, [TP_PRODUCT])
        assert chosen[0]["name"] == "trustpilot-product-reviews 901 (2).xlsx"

    def test_missing_pattern_still_raises(self):
        items = [self._item("TrustPilot Service Reviews.xlsx")]
        with pytest.raises(RuntimeError, match="no file matching"):
            select_items_by_patterns(items, [TP_PRODUCT])

    def test_folders_ignored(self):
        items = [{"name": "trustpilot product stuff.xlsx", "folder": {}}]
        with pytest.raises(RuntimeError):
            select_items_by_patterns(items, [TP_PRODUCT])


def _sources_config(tmp_path):
    return SourceFiles(reviews_dir=tmp_path)


class TestSourceLoaders:
    def test_trustpilot_product_mapping(self, tmp_path):
        cfg = _sources_config(tmp_path)
        pd.DataFrame([{"review_id": "r1", "stars": 5, "product_name": "Lamp",
                       "product_sku": "U25090", "content": "Great light",
                       "consumer_name": "Pat", "created_at": "2026-01-01"}]
                     ).to_excel(tmp_path / "trustpilot-product-reviews.xlsx", index=False)
        out = TrustpilotProductSource(cfg).fetch()
        assert out == [{"review_id": "r1", "review_type": "product",
                        "review_source": "trustpilot", "rating": 5,
                        "product_name": "Lamp", "product_sku": "U25090",
                        "text": "Great light", "reviewer_name": "Pat",
                        "review_date": "2026-01-01"}]

    def test_trustpilot_service_title_merged_into_text(self, tmp_path):
        cfg = _sources_config(tmp_path)
        pd.DataFrame([{"Review Id": "s1", "Review Stars": 4,
                       "Review Title": "Great service!", "Review Content": "Fast reply.",
                       "Review Username": "Sam", "Review Created (UTC)": "2026-02-02"}]
                     ).to_excel(tmp_path / "TrustPilot Service Reviews.xlsx", index=False)
        out = TrustpilotServiceSource(cfg).fetch()
        assert out[0]["review_type"] == "service"
        assert out[0]["text"] == "Great service!\n\nFast reply."
        assert out[0]["title"] == "Great service!"

    def test_amazon_error_rows_and_duplicates_dropped(self, tmp_path):
        cfg = _sources_config(tmp_path)
        pd.DataFrame([
            {"review_id": "a1", "rating": 5, "product_name": "Lamp", "asin": "B01",
             "model_number": "u25090", "review_country": "us", "review_header": "Wow",
             "review_text": "Bright", "author_name": "A", "review_posted_date": "2026-03-03",
             "is_verified": True, "error": None},
            {"review_id": "a1", "rating": 1, "product_name": "Lamp", "asin": "B01",
             "model_number": "u25090", "review_country": "us", "review_header": "Dup",
             "review_text": "dup row", "author_name": "A", "review_posted_date": "2026-03-03",
             "is_verified": True, "error": None},
            {"review_id": "a2", "rating": None, "product_name": None, "asin": None,
             "model_number": None, "review_country": None, "review_header": None,
             "review_text": None, "author_name": None, "review_posted_date": None,
             "is_verified": None, "error": "blocked"},
        ]).to_csv(tmp_path / "amazon_product_reviews_all.csv", index=False)
        out = AmazonBrightDataSource(cfg).fetch()
        assert len(out) == 1  # error row + duplicate both dropped
        assert out[0]["model_number"] == "U25090"  # uppercased
        assert out[0]["text"] == "Wow\n\nBright"
        assert out[0]["verified_purchase"] is True

    def test_loader_matches_any_pattern_conforming_name(self, tmp_path):
        # There's no expected filename at all -- any name matching the
        # pattern (a "(2)" suffix, a fresh row count, whatever) works.
        cfg = _sources_config(tmp_path)
        pd.DataFrame([{"review_id": "r1", "stars": 3, "product_name": "L",
                       "product_sku": "", "content": "ok", "consumer_name": "",
                       "created_at": ""}]
                     ).to_excel(tmp_path / "trustpilot-product-reviews 901 (2).xlsx",
                                index=False)
        out = TrustpilotProductSource(cfg).fetch()
        assert len(out) == 1


class TestLoadReviews:
    def test_uid_assignment_and_row_fallback(self):
        class Fake:
            name = "fake"
            def __init__(self, reviews):
                self._r = reviews
            def fetch(self):
                return self._r

        reviews = load_reviews([
            Fake([{"review_id": "x", "review_type": "product", "review_source": "amazon"}]),
            Fake([{"review_id": "", "review_type": "service", "review_source": "trustpilot"}]),
        ])
        assert reviews[0]["uid"] == "amazon:product:x"
        assert reviews[1]["uid"] == "trustpilot:service:row1"

    def test_build_default_sources_order(self, tmp_path):
        sources = build_default_sources(_sources_config(tmp_path))
        assert [s.name for s in sources] == [
            "trustpilot_product", "trustpilot_service", "amazon_brightdata"]
