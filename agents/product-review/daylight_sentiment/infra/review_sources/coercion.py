"""Value coercion for raw export cells -- the exports carry NaN in numeric
and text columns alike."""
from __future__ import annotations

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
