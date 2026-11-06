import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

WINDOW_NAMES = ("TRAIN_END", "TEST_START", "TEST_END", "FORECAST_START", "FORECAST_END",
                "BENCHMARK_START", "BENCHMARK_END")


@pytest.fixture(autouse=True)
def restore_windows():
    """main.py overrides config in memory, so put it back after every test."""
    saved = {name: getattr(config, name) for name in WINDOW_NAMES}
    yield
    for name, value in saved.items():
        setattr(config, name, value)


def make_raw(last_month: str, *, first_month: str = "2022-01", txns: int = 100,
             overrides: dict[str, int] | None = None) -> pd.DataFrame:
    """A steady extract: three items, every month, non-Amazon.

    `overrides` sets a month's transactions per item, to fake a partial month.
    """
    overrides = overrides or {}
    rows = []
    for month in pd.period_range(first_month, last_month, freq="M").strftime("%Y-%m"):
        for item in ("D1", "E1", "U1"):
            rows.append({"item_code": item, "year_month": month, "channel": "Direct",
                         "monthly_qty": "10", "transaction_count": str(overrides.get(month, txns))})
    return pd.DataFrame(rows)
