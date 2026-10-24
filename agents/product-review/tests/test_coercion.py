"""Characterization: value-coercion helpers (_clean_str, _safe_int, _safe_bool,
_clean_date)."""
import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from daylight_sentiment.infra.review_sources.coercion import (
    _clean_date,
    _clean_str,
    _safe_bool,
    _safe_int,
)


class TestCleanStr:
    def test_none_is_empty(self):
        assert _clean_str(None) == ""

    def test_nan_is_empty(self):
        assert _clean_str(float("nan")) == ""

    def test_nan_like_strings_are_empty(self):
        assert _clean_str("nan") == ""
        assert _clean_str("NaT") == ""
        assert _clean_str("None") == ""
        assert _clean_str("  none  ") == ""

    def test_strips_whitespace(self):
        assert _clean_str("  hello  ") == "hello"

    def test_numbers_stringified(self):
        assert _clean_str(42) == "42"
        assert _clean_str(3.5) == "3.5"

    def test_containers_stringified_not_nan_checked(self):
        assert _clean_str([1, 2]) == "[1, 2]"
        assert _clean_str({"a": 1}) == "{'a': 1}"


class TestSafeInt:
    def test_none_and_nan(self):
        assert _safe_int(None) is None
        assert _safe_int(float("nan")) is None

    def test_float_string_truncates(self):
        assert _safe_int("3.7") == 3
        assert _safe_int(4.9) == 4

    def test_garbage_is_none(self):
        assert _safe_int("five stars") is None
        assert _safe_int("") is None

    def test_plain_int(self):
        assert _safe_int(5) == 5
        assert _safe_int("5") == 5


class TestSafeBool:
    def test_none_and_nan(self):
        assert _safe_bool(None) is None
        assert _safe_bool(float("nan")) is None

    def test_truthiness_semantics(self):
        # CHARACTERIZATION: plain bool() semantics -- "" and 0 are False,
        # any non-empty string (including "false") is True.
        assert _safe_bool(0) is False
        assert _safe_bool("") is False
        assert _safe_bool(1) is True
        assert _safe_bool("false") is True
        assert _safe_bool(True) is True


class TestCleanDate:
    def test_text_is_kept_exactly(self):
        assert _clean_date("2026-10-07T17:24:58.498Z") == "2026-10-07T17:24:58.498Z"
        assert _clean_date(" 7 October 2026 ") == "7 October 2026"
        assert _clean_date("Reviewed in the United States on October 7, 2026",
                           with_time=False) == (
            "Reviewed in the United States on October 7, 2026")

    def test_blank_is_empty(self):
        assert _clean_date(None) == ""
        assert _clean_date(float("nan")) == ""
        assert _clean_date(pd.NaT) == ""
        assert _clean_date("") == ""

    def test_datetime_written_like_the_csv_export(self):
        assert _clean_date(datetime(2026, 10, 7, 17, 24, 58, 498000)) == (
            "2026-10-07T17:24:58.498Z")
        assert _clean_date(pd.Timestamp("2026-10-07 17:24:58.498")) == (
            "2026-10-07T17:24:58.498Z")
        assert _clean_date(datetime(2026, 10, 7)) == "2026-10-07T00:00:00.000Z"

    def test_sub_millisecond_noise_is_rounded(self):
        # Excel stores a fraction of a day; a read can come back a few
        # microseconds off the millisecond the export wrote.
        assert _clean_date(datetime(2026, 10, 7, 17, 24, 58, 497999)) == (
            "2026-10-07T17:24:58.498Z")

    def test_timezone_aware_converted_to_utc(self):
        cest = timezone(timedelta(hours=2))
        assert _clean_date(datetime(2026, 10, 7, 19, 24, 58, 498000, tzinfo=cest)) == (
            "2026-10-07T17:24:58.498Z")

    def test_date_only(self):
        assert _clean_date(datetime(2026, 10, 7, 17, 24), with_time=False) == "2026-10-07"
        assert _clean_date(date(2026, 10, 7), with_time=False) == "2026-10-07"
        assert _clean_date(date(2026, 10, 7)) == "2026-10-07T00:00:00.000Z"
