import pandas as pd
import pytest

import config
from analysis.window_proposal import propose, verify_offsets


def test_development_window_keeps_its_offsets():
    assert propose("2026-09") == {
        "TRAIN_END": "2026-03", "TEST_START": "2026-04", "TEST_END": "2026-09",
        "FORECAST_START": "2026-10", "FORECAST_END": "2027-03",
    }


def test_production_window_trains_to_the_anchor():
    assert propose("2026-09", production=True) == {
        "TRAIN_END": "2026-09", "TEST_START": "2026-10", "TEST_END": "2026-09",
        "FORECAST_START": "2026-10", "FORECAST_END": "2027-03",
    }


def test_both_modes_forecast_the_same_months():
    dev, prod = propose("2026-12"), propose("2026-12", production=True)
    assert (dev["FORECAST_START"], dev["FORECAST_END"]) == ("2027-01", "2027-06")
    assert (prod["FORECAST_START"], prod["FORECAST_END"]) == ("2027-01", "2027-06")


def test_production_test_window_is_empty():
    window = propose("2026-09", production=True)
    assert window["TEST_START"] > window["TEST_END"]
    months = pd.date_range(window["TEST_START"] + "-01", window["TEST_END"] + "-01", freq="MS")
    assert len(months) == 0


def test_static_config_still_follows_the_development_offsets():
    assert verify_offsets() == []


def test_apply_production_window_sets_config_and_model_constants():
    main = pytest.importorskip("main")
    from models import prophet_model
    from tests.conftest import make_raw

    assert main.apply_production_window(make_raw("2026-09")) is True
    assert config.TRAIN_END == config.TEST_END == "2026-09"
    assert config.FORECAST_START == "2026-10"
    assert prophet_model.TRAIN_END_TS == pd.Timestamp("2026-09-01")


def test_production_ignores_the_static_test_end():
    """A static TEST_END ahead of the data must not pin the window to it."""
    main = pytest.importorskip("main")
    from tests.conftest import make_raw

    config.TEST_END = "2030-01"
    assert main.apply_production_window(make_raw("2026-09")) is True
    assert config.TEST_END == "2026-09"


def test_naive_route_gives_no_test_rows_in_production():
    pytest.importorskip("prophet")
    from models import naive_model

    for name, value in propose("2026-09", production=True).items():
        setattr(config, name, value)
    months = pd.date_range("2025-10-01", "2026-09-01", freq="MS")
    pooled = pd.DataFrame({"item_code": "A1", "ds": months, "y": 12.0})
    routing = pd.DataFrame({"family_key": ["A1"], "route": ["Naive-3mo"]})

    test_df, forecast_df = naive_model.run_naive_forecasts(pooled, routing)

    assert test_df.empty
    assert len(forecast_df) == 6
    assert forecast_df["yhat"].eq(12.0).all()
