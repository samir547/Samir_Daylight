import numpy as np
import pandas as pd
import pytest

from order_forecast.backtest import forecast_series, make_origins, run_backtest
from order_forecast.baselines import METHODS
from order_forecast.series import month_range, to_wide
from order_forecast.snapshot import Snapshot

MONTHS = month_range("2023-09-01", "2026-08-01")      # 36 months


def test_origins():
    assert make_origins(36, 24, 6, 6) == [24, 30]
    assert len(make_origins(36, 24, 6, 1)) == 7
    assert make_origins(30, 24, 6, 6) == [24]
    assert make_origins(20, 24, 6, 6) == []


def test_wide_masks_before_first_and_zero_fills_gaps():
    long = pd.DataFrame({"asin": ["A", "A", "B"], "month_start": MONTHS[[2, 5, 0]], "u": [3, 4, 9]})
    w = to_wide(long, "asin", "u", MONTHS, mask_before_first=True)
    assert np.isnan(w.loc["A"].iloc[:2]).all()
    assert w.loc["A"].iloc[2] == 3 and w.loc["A"].iloc[3] == 0 and w.loc["A"].iloc[5] == 4
    w2 = to_wide(long, "asin", "u", MONTHS, mask_before_first=False)
    assert w2.loc["A"].iloc[0] == 0 and not w2.isna().any().any()


def test_no_forecast_sees_data_after_its_origin():
    rng = np.random.default_rng(1)
    vals = rng.integers(0, 50, size=(3, 36)).astype(float)
    wide = pd.DataFrame(vals, index=["A", "B", "C"], columns=MONTHS)
    base = forecast_series(wide, [24], 6, METHODS, 12)
    tampered = wide.copy()
    tampered.iloc[:, 24:] = 1e9                       # change everything from the origin on
    again = forecast_series(tampered, [24], 6, METHODS, 12)
    pd.testing.assert_frame_equal(base, again)


def test_short_series_are_skipped():
    vals = np.full((2, 36), 5.0)
    vals[1, :30] = np.nan                              # B only starts at month 30
    wide = pd.DataFrame(vals, index=["A", "B"], columns=MONTHS)
    pred = forecast_series(wide, [24, 30], 6, METHODS, 12)
    assert set(pred["asin"]) == {"A"}


def _snapshot():
    a = pd.DataFrame({"asin": "A", "month_start": MONTHS, "ordered_units": 10.0, "shipped_units": 10.0})
    b_months = MONTHS[10:]
    b = pd.DataFrame({"asin": "B", "month_start": b_months, "ordered_units": 4.0, "shipped_units": 4.0})
    sales = pd.concat([a, b], ignore_index=True)
    inv = sales.rename(columns={"ordered_units": "net_received_units"})[["asin", "month_start", "net_received_units"]]
    total = sales.groupby("month_start")["ordered_units"].sum()
    invoices = pd.DataFrame({"marketplace": "US", "product_code": "X1", "month_start": total.index,
                             "gross_units": total.values, "credit_units": 0.0})
    return Snapshot("US", sales, inv, invoices, {})


def test_end_to_end_on_a_flat_series_is_exact():
    r = run_backtest(_snapshot(), horizon=6, min_train=24, min_history=12)
    s = r["scores"]
    flat = s[s["method"].isin(["mean3", "mean6", "mean12", "naive_last", "prior_year", "ses"])]
    assert (flat["wape"].dropna() < 1e-9).all()
    assert set(s["answer_key"]) == {"sellout", "receipts", "invoices"}
    assert r["coverage"] == pytest.approx(1.0)
    assert r["origins"] == [MONTHS[23], MONTHS[29]]


def test_too_few_months_raises():
    snap = _snapshot()
    snap.sales = snap.sales[snap.sales["month_start"] < MONTHS[20]]
    with pytest.raises(ValueError):
        run_backtest(snap, min_train=24)


def test_loading_a_results_folder_gives_a_clear_error(tmp_path):
    from order_forecast.snapshot import load_snapshot
    (tmp_path / "scores.csv").write_text("x")
    with pytest.raises(SystemExit, match="results folder"):
        load_snapshot(tmp_path)
