import numpy as np
import pandas as pd

from order_forecast.backtest import forecast_series
from order_forecast.baselines import METHODS
from order_forecast.seasonal import forecast_seasonal, seasonal_factors
from order_forecast.series import month_range

MONTHS = month_range("2023-09-01", "2026-08-01")      # 36 months
SHAPE = np.array([0.8, 0.8, 0.9, 1.0, 1.0, 0.9, 0.9, 1.0, 1.1, 1.1, 1.4, 1.6])   # Jan..Dec, mean 1.0


def make_wide(n_asins=5):
    base = np.array([m.month for m in MONTHS])
    rows = [100.0 * (i + 1) * SHAPE[base - 1] for i in range(n_asins)]
    return pd.DataFrame(rows, index=[f"A{i}" for i in range(n_asins)], columns=MONTHS)


def test_factors_recover_the_shape_and_average_one():
    total = make_wide().sum()
    f = seasonal_factors(total)
    assert abs(f.mean() - 1.0) < 1e-9
    assert f[10] > 1.25 and f[11] > 1.4 and f[0] < 0.9           # Nov, Dec high, Jan low
    assert np.allclose(seasonal_factors(total, shrink=0.0), 1.0)


def test_seasonal_beats_flat_average_on_seasonal_data():
    wide = make_wide()
    seas = forecast_seasonal(wide, [24], 6, 12)
    flat = forecast_series(wide, [24], 6, {"mean12": METHODS["mean12"]}, 12)
    act = wide.stack().rename("actual").reset_index().rename(columns={"level_0": "asin", "level_1": "target_month"})
    def err(p):
        j = p.merge(act, on=["asin", "target_month"])
        return (j.predicted - j.actual).abs().sum() / j.actual.sum()
    assert err(seas) < 0.5 * err(flat)


def test_no_peeking_past_the_origin():
    wide = make_wide()
    tampered = wide.copy()
    tampered.iloc[:, 24:] = 1e9
    pd.testing.assert_frame_equal(forecast_seasonal(wide, [24], 6, 12), forecast_seasonal(tampered, [24], 6, 12))
