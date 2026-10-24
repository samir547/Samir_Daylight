"""Value coercion for raw export cells -- the exports carry NaN in numeric
and text columns alike."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

import pandas as pd


def _clean_str(value: Any) -> str:
    """Stringify a cell, mapping pandas NaN/NaT to an empty string."""
    if value is None:
        return ""
    if not isinstance(value, (list, dict, tuple)):
        try:
            if pd.isna(value):
                return ""
        except (TypeError, ValueError):
            pass
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat", "none"} else text


def _clean_date(value: Any, with_time: bool = True) -> str:
    """A date cell as the text stored in review_date.

    Text -- what a CSV export always gives, and what an Excel cell holding
    text gives -- is kept exactly as it is. A real date cell (Excel) is
    written the way the CSV exports write the same moment, so both formats
    store the same text: ISO 8601 in UTC with milliseconds and a Z
    (2026-10-07T17:24:58.498Z), which vw_review_analysis parses with style
    127; or, with ``with_time=False``, just the day (2026-10-07). A naive
    datetime is taken to be UTC already, as the exports' timestamps are."""
    if not _clean_str(value):
        return ""
    if isinstance(value, datetime):
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert(timezone.utc).tz_localize(None)
        stamp = stamp.round("ms")
        if not with_time:
            return stamp.strftime("%Y-%m-%d")
        return stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"
    if isinstance(value, date):
        return value.isoformat() + ("T00:00:00.000Z" if with_time else "")
    return _clean_str(value)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None or pd.isna(value):
            return None
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_bool(value: Any) -> Optional[bool]:
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return bool(value)
