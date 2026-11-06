import sys

import pandas as pd
import pytest

from analysis.month_completeness import latest_elapsed_complete_month
from tests.conftest import make_raw


def anchor(raw, today):
    return latest_elapsed_complete_month(raw, pd.Timestamp(today))[0]


def test_elapsed_complete_month_is_the_anchor():
    assert anchor(make_raw("2026-09"), "2026-10-05") == "2026-09"


def test_current_month_is_rejected_even_if_its_ratio_passes():
    assert anchor(make_raw("2026-09"), "2026-09-29") == "2026-08"


def test_month_counts_as_elapsed_on_the_first_of_the_next_month():
    assert anchor(make_raw("2026-09"), "2026-10-01") == "2026-09"


def test_partial_month_is_rejected_by_the_completeness_check():
    raw = make_raw("2026-10", overrides={"2026-10": 20})
    assert anchor(raw, "2026-11-05") == "2026-09"


def test_no_qualifying_month_raises():
    with pytest.raises(ValueError, match="complete and fully elapsed"):
        anchor(make_raw("2026-09", first_month="2026-09"), "2026-09-15")


def test_empty_extract_raises():
    with pytest.raises(ValueError):
        latest_elapsed_complete_month(make_raw("2026-09").iloc[0:0], pd.Timestamp("2026-10-05"))


def test_production_run_exits_3_when_no_month_qualifies(monkeypatch, tmp_path):
    main = pytest.importorskip("main")
    import config

    def no_anchor(raw):
        raise ValueError("nothing qualifies")

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(main, "load_all", lambda cached: (make_raw("2026-09"), None, None, None, None))
    monkeypatch.setattr(main, "latest_elapsed_complete_month", no_anchor)
    monkeypatch.setattr(sys, "argv", ["main.py", "--production", "--cached"])

    assert main.main() == 3
