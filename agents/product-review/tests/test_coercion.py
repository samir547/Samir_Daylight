"""Characterization: value-coercion helpers (_clean_str, _safe_int, _safe_bool)."""
import math

from daylight_sentiment.infra.review_sources.coercion import _clean_str, _safe_bool, _safe_int


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
