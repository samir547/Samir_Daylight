"""ISSUE-01/02: FilePattern matching, local file resolution, SharePoint
pattern selection (pure, no network), source loaders (synthetic files), and
load_reviews() uid assignment."""
import re
import time
from datetime import date, datetime

import pandas as pd
import pytest

from daylight_sentiment.config import FilePattern, SourceFiles
from daylight_sentiment.domain.action_items import _as_date
from daylight_sentiment.infra.review_sources import (
    AmazonBrightDataSource,
    TrustpilotProductSource,
    TrustpilotServiceSource,
    build_default_sources,
    load_reviews,
)
from daylight_sentiment.infra.review_sources.base import (
    MissingColumnsError,
    read_table,
    resolve_source_file,
)
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

    def test_every_source_accepts_xlsx_and_csv_by_default(self):
        src = SourceFiles()
        for pattern, stem in [(src.trustpilot_product_pattern, "trustpilot-product-reviews"),
                              (src.trustpilot_service_pattern, "TrustPilot Service Reviews"),
                              (src.amazon_product_pattern, "amazon_product_reviews_all")]:
            assert pattern.matches(f"{stem}.xlsx")
            assert pattern.matches(f"{stem}.CSV")
            assert not pattern.matches(f"{stem}.xls")
            assert not pattern.matches(f"{stem}.csv.bak")

    def test_describe(self):
        assert SourceFiles().trustpilot_product_pattern.describe() == (
            "*trustpilot*product*.{xlsx,csv}")
        assert TP_PRODUCT.describe() == "*trustpilot*product*.xlsx"

    def test_single_extension_string_is_accepted(self):
        assert TP_PRODUCT.extensions == (".xlsx",)
        assert FilePattern(("a",), (".CSV",)).extensions == (".csv",)


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

    def test_newest_wins_across_xlsx_and_csv(self, tmp_path):
        import os
        pattern = SourceFiles().trustpilot_product_pattern
        xlsx = tmp_path / "trustpilot-product-reviews 880.xlsx"
        csv = tmp_path / "trustpilot-product-reviews 901.csv"
        xlsx.write_bytes(b"x")
        csv.write_bytes(b"y")
        now = time.time()
        os.utime(xlsx, (now - 100, now - 100))
        os.utime(csv, (now, now))
        assert resolve_source_file(pattern, tmp_path) == csv
        os.utime(xlsx, (now + 100, now + 100))
        assert resolve_source_file(pattern, tmp_path) == xlsx

    def test_directories_named_like_a_match_are_ignored(self, tmp_path):
        (tmp_path / "trustpilot product.csv").mkdir()
        with pytest.raises(FileNotFoundError):
            resolve_source_file(SourceFiles().trustpilot_product_pattern, tmp_path)

    def test_missing_directory_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No file in .* matches"):
            resolve_source_file(TP_PRODUCT, tmp_path / "absent")

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

    def test_newest_wins_across_xlsx_and_csv(self):
        pattern = SourceFiles().amazon_product_pattern
        items = [self._item("amazon product reviews.csv", "2026-09-01T00:00:00Z"),
                 self._item("amazon product reviews.xlsx", "2026-09-15T00:00:00Z")]
        assert select_items_by_patterns(items, [pattern])[0]["name"] == (
            "amazon product reviews.xlsx")
        items[0]["lastModifiedDateTime"] = "2026-09-20T00:00:00Z"
        assert select_items_by_patterns(items, [pattern])[0]["name"] == (
            "amazon product reviews.csv")

    def test_no_match_message_names_both_extensions(self):
        with pytest.raises(RuntimeError, match=r"\*amazon\*product\*\.\{xlsx,csv\}"):
            select_items_by_patterns([], [SourceFiles().amazon_product_pattern])

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

    def test_trustpilot_product_from_csv(self, tmp_path):
        cfg = _sources_config(tmp_path)
        pd.DataFrame([{"review_id": "r1", "stars": 5, "product_name": "Lamp",
                       "product_sku": "U25090", "content": "Great light",
                       "consumer_name": "Pat", "created_at": "2026-01-01"}]
                     ).to_csv(tmp_path / "trustpilot-product-reviews.csv", index=False)
        out = TrustpilotProductSource(cfg).fetch()
        assert out[0]["review_id"] == "r1" and out[0]["rating"] == 5

    def test_amazon_from_xlsx(self, tmp_path):
        cfg = _sources_config(tmp_path)
        pd.DataFrame([{"review_id": "a1", "rating": 4, "product_name": "Lamp", "asin": "B01",
                       "model_number": "u25090", "review_country": "us",
                       "review_header": "Wow", "review_text": "Bright", "author_name": "A",
                       "review_posted_date": "2026-03-03", "is_verified": False}]
                     ).to_excel(tmp_path / "amazon_product_reviews_all.xlsx", index=False)
        out = AmazonBrightDataSource(cfg).fetch()
        assert out[0]["rating"] == 4
        assert out[0]["verified_purchase"] is False

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


class TestReadTable:
    def test_reads_by_real_extension(self, tmp_path):
        df = pd.DataFrame([{"a": "x", "b": 1}])
        df.to_excel(tmp_path / "t.XLSX", index=False)
        df.to_csv(tmp_path / "t.csv", index=False)
        for name in ("t.XLSX", "t.csv"):
            assert read_table(tmp_path / name).to_dict("records") == [{"a": "x", "b": 1}]

    def test_csv_content_in_a_file_named_xlsx_is_not_guessed(self, tmp_path):
        # The extension decides the reader; a mislabelled file fails loudly.
        (tmp_path / "t.xlsx").write_text("a,b\nx,1\n", encoding="utf-8")
        with pytest.raises(Exception):
            read_table(tmp_path / "t.xlsx")

    def test_unsupported_extension_raises(self, tmp_path):
        path = tmp_path / "t.xls"
        path.write_bytes(b"")
        with pytest.raises(ValueError, match=r"t\.xls.*expected \.xlsx or \.csv"):
            read_table(path)


TP_PRODUCT_ROW = {"review_id": "r1", "stars": 5, "product_name": "Lamp",
                  "product_sku": "U25090", "content": "Great light",
                  "consumer_name": "Pat", "created_at": "2026-01-01"}
TP_SERVICE_ROW = {"Review Id": "s1", "Review Stars": 4, "Review Title": "Great",
                  "Review Content": "Fast reply.", "Review Username": "Sam",
                  "Review Created (UTC)": "2026-02-02"}
AMAZON_ROW = {"review_id": "a1", "rating": 4, "product_name": "Lamp", "asin": "B01",
              "model_number": "u25090", "review_country": "us", "review_header": "Wow",
              "review_text": "Bright", "author_name": "A",
              "review_posted_date": "2026-03-03", "is_verified": True}


class TestMissingColumns:
    @pytest.mark.parametrize("ext", ["xlsx", "csv"])
    @pytest.mark.parametrize("source_cls, filename, row, dropped", [
        (TrustpilotProductSource, "trustpilot-product-reviews", TP_PRODUCT_ROW,
         ["product_sku", "created_at"]),
        (TrustpilotServiceSource, "TrustPilot Service Reviews", TP_SERVICE_ROW,
         ["Review Created (UTC)"]),
        (AmazonBrightDataSource, "amazon_product_reviews_all", AMAZON_ROW,
         ["is_verified", "review_country"]),
    ])
    def test_missing_columns_fail_naming_file_and_columns(
            self, tmp_path, ext, source_cls, filename, row, dropped):
        df = pd.DataFrame([{k: v for k, v in row.items() if k not in dropped}])
        path = tmp_path / f"{filename}.{ext}"
        if ext == "xlsx":
            df.to_excel(path, index=False)
        else:
            df.to_csv(path, index=False)
        with pytest.raises(MissingColumnsError) as exc:
            source_cls(_sources_config(tmp_path)).fetch()
        message = str(exc.value)
        assert path.name in message
        named = message.split("missing required column(s): ")[1].split(", ")
        assert sorted(named) == sorted(dropped)

    def test_header_only_file_with_every_column_loads_nothing(self, tmp_path):
        pd.DataFrame(columns=list(TP_SERVICE_ROW)).to_csv(
            tmp_path / "TrustPilot Service Reviews.csv", index=False)
        assert TrustpilotServiceSource(_sources_config(tmp_path)).fetch() == []

    def test_empty_csv_names_every_column(self, tmp_path):
        (tmp_path / "amazon_product_reviews_all.csv").write_text(
            "unrelated\n", encoding="utf-8")
        with pytest.raises(MissingColumnsError, match="review_id, rating, .*is_verified"):
            AmazonBrightDataSource(_sources_config(tmp_path)).fetch()

    def test_amazon_error_column_is_optional(self, tmp_path):
        pd.DataFrame([AMAZON_ROW]).to_csv(
            tmp_path / "amazon_product_reviews_all.csv", index=False)
        assert len(AmazonBrightDataSource(_sources_config(tmp_path)).fetch()) == 1


def _load_both_ways(tmp_path, source_cls, stem, rows):
    """Write the same table as .xlsx and as .csv (each in its own folder),
    load each through the source, and return (from_xlsx, from_csv).
    dtype=object keeps an int column with blanks as ints in the written
    files, as a real export has them (pandas would otherwise write 35000.0)."""
    df = pd.DataFrame(rows, dtype=object)
    out = []
    for ext in ("xlsx", "csv"):
        folder = tmp_path / ext
        folder.mkdir()
        if ext == "xlsx":
            df.to_excel(folder / f"{stem}.xlsx", index=False)
        else:
            df.to_csv(folder / f"{stem}.csv", index=False)
        out.append(source_cls(_sources_config(folder)).fetch())
    return tuple(out)


BLANK = None


class TestXlsxCsvParity:
    """The same data supplied as .xlsx and as .csv gives identical reviews:
    ids/SKUs as text (leading zeros kept, no "123.0"), ratings as int, empty
    cells as "", fully blank rows ignored."""

    def test_trustpilot_product(self, tmp_path):
        rows = [
            {"review_id": "00123", "stars": 5, "product_name": "Lamp", "product_sku": 35000,
             "content": "Great light", "consumer_name": "Pat",
             "created_at": "2026-10-07T17:24:58.498Z"},
            dict.fromkeys(TP_PRODUCT_ROW, BLANK),
            {"review_id": "60f0321651", "stars": 2, "product_name": BLANK,
             "product_sku": BLANK, "content": "Flickers", "consumer_name": BLANK,
             "created_at": BLANK},
        ]
        from_xlsx, from_csv = _load_both_ways(
            tmp_path, TrustpilotProductSource, "trustpilot-product-reviews", rows)
        assert from_xlsx == from_csv
        assert [r["review_id"] for r in from_csv] == ["00123", "60f0321651"]
        assert [r["product_sku"] for r in from_csv] == ["35000", ""]
        assert [r["rating"] for r in from_csv] == [5, 2]
        assert all(type(r["rating"]) is int for r in from_csv)
        assert from_csv[1]["product_name"] == "" and from_csv[1]["review_date"] == ""

    def test_trustpilot_service(self, tmp_path):
        rows = [
            {"Review Id": 98765, "Review Stars": 4, "Review Title": "Great",
             "Review Content": "Fast reply.", "Review Username": "Sam",
             "Review Created (UTC)": "2026-10-07T17:24:58.498Z"},
            dict.fromkeys(TP_SERVICE_ROW, BLANK),
            {"Review Id": "0042", "Review Stars": 1, "Review Title": BLANK,
             "Review Content": "Slow.", "Review Username": BLANK,
             "Review Created (UTC)": BLANK},
        ]
        from_xlsx, from_csv = _load_both_ways(
            tmp_path, TrustpilotServiceSource, "TrustPilot Service Reviews", rows)
        assert from_xlsx == from_csv
        assert [r["review_id"] for r in from_csv] == ["98765", "0042"]
        assert [r["rating"] for r in from_csv] == [4, 1]
        assert from_csv[1]["title"] == "" and from_csv[1]["text"] == "Slow."

    def test_amazon(self, tmp_path):
        rows = [
            {**AMAZON_ROW, "review_id": "R1ABC", "model_number": "u25090",
             "asin": "B0123", "is_verified": True},
            dict.fromkeys(AMAZON_ROW, BLANK),
            {**AMAZON_ROW, "review_id": "R2DEF", "rating": 3, "model_number": 1410,
             "review_header": BLANK, "review_country": BLANK, "is_verified": BLANK},
            {**AMAZON_ROW, "review_id": "R3GHI", "rating": 1, "is_verified": False},
        ]
        from_xlsx, from_csv = _load_both_ways(
            tmp_path, AmazonBrightDataSource, "amazon_product_reviews_all", rows)
        assert from_xlsx == from_csv
        assert [r["review_id"] for r in from_csv] == ["R1ABC", "R2DEF", "R3GHI"]
        assert [r["model_number"] for r in from_csv] == ["U25090", "1410", "U25090"]
        assert [r["rating"] for r in from_csv] == [4, 3, 1]
        assert [r["verified_purchase"] for r in from_csv] == [True, None, False]
        assert from_csv[1]["review_country"] == "" and from_csv[1]["title"] == ""

    def test_csv_with_byte_order_mark(self, tmp_path):
        path = tmp_path / "TrustPilot Service Reviews.csv"
        pd.DataFrame([TP_SERVICE_ROW]).to_csv(path, index=False, encoding="utf-8-sig")
        [review] = TrustpilotServiceSource(_sources_config(tmp_path)).fetch()
        assert review["review_id"] == "s1"


def _write(folder, name, rows):
    folder.mkdir()
    df = pd.DataFrame(rows, dtype=object)
    if name.endswith(".xlsx"):
        df.to_excel(folder / name, index=False)
    else:
        df.to_csv(folder / name, index=False)


ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class TestReviewDates:
    """review_date is stored as text and parsed later (vw_review_analysis,
    then _as_date on review_date_parsed). A real Excel date cell must end up
    as the same text the CSV export carries for that moment."""

    MOMENT = datetime(2026, 10, 7, 17, 24, 58, 498000)

    @pytest.mark.parametrize("source_cls, stem, row, date_col", [
        (TrustpilotProductSource, "trustpilot-product-reviews", TP_PRODUCT_ROW, "created_at"),
        (TrustpilotServiceSource, "TrustPilot Service Reviews", TP_SERVICE_ROW,
         "Review Created (UTC)"),
    ])
    def test_trustpilot_excel_datetime_matches_csv_text(
            self, tmp_path, source_cls, stem, row, date_col):
        _write(tmp_path / "x", f"{stem}.xlsx", [{**row, date_col: self.MOMENT}])
        _write(tmp_path / "c", f"{stem}.csv", [{**row, date_col: "2026-10-07T17:24:58.498Z"}])
        [from_xlsx] = source_cls(_sources_config(tmp_path / "x")).fetch()
        [from_csv] = source_cls(_sources_config(tmp_path / "c")).fetch()
        assert from_xlsx == from_csv
        assert from_csv["review_date"] == "2026-10-07T17:24:58.498Z"
        assert ISO_UTC.match(from_xlsx["review_date"])  # the style-127 shape

    def test_trustpilot_excel_cell_holding_text_is_untouched(self, tmp_path):
        # Today's real Trustpilot .xlsx exports hold the timestamp as text.
        _write(tmp_path / "x", "trustpilot-product-reviews.xlsx",
               [{**TP_PRODUCT_ROW, "created_at": "2021-07-15T13:03:18.514Z"}])
        [review] = TrustpilotProductSource(_sources_config(tmp_path / "x")).fetch()
        assert review["review_date"] == "2021-07-15T13:03:18.514Z"

    def test_amazon_excel_datetime_stored_as_day(self, tmp_path):
        _write(tmp_path / "x", "amazon_product_reviews_all.xlsx",
               [{**AMAZON_ROW, "review_posted_date": self.MOMENT}])
        [review] = AmazonBrightDataSource(_sources_config(tmp_path / "x")).fetch()
        assert review["review_date"] == "2026-10-07"

    def test_amazon_text_dates_kept_as_exported(self, tmp_path):
        text = "Reviewed in the United States on October 7, 2026"
        rows = [{**AMAZON_ROW, "review_posted_date": text}]
        from_xlsx, from_csv = _load_both_ways(
            tmp_path, AmazonBrightDataSource, "amazon_product_reviews_all", rows)
        assert from_xlsx[0]["review_date"] == from_csv[0]["review_date"] == text

    @pytest.mark.parametrize("stored, expected", [
        ("2026-10-07T17:24:58.498Z", date(2026, 10, 7)),   # CSV / normalised Excel
        ("2026-10-07 17:24:58.498000", date(2026, 10, 7)),  # str() of an Excel datetime
        ("2026-10-07", date(2026, 10, 7)),
        (datetime(2026, 10, 7, 17, 24), date(2026, 10, 7)),  # DATE column from the view
        (date(2026, 10, 7), date(2026, 10, 7)),
        ("Reviewed in the United States on October 7, 2026", None),
        ("", None),
    ])
    def test_as_date_reads_both_forms_to_the_same_day(self, stored, expected):
        assert _as_date(stored) == expected


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
