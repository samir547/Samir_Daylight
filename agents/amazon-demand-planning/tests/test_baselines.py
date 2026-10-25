import numpy as np

from order_forecast.baselines import METHODS, naive_last, prior_year, ses


def test_means_and_naive():
    h = np.arange(1.0, 25.0)
    assert (METHODS["mean3"](h, 2) == [23.0, 23.0]).all()
    assert (METHODS["mean12"](h, 1) == [18.5]).all()
    assert (naive_last(h, 3) == [24.0] * 3).all()


def test_prior_year_uses_the_same_month_last_year():
    h = np.arange(100.0, 124.0)            # 24 months, value = 100 + index
    f = prior_year(h, 3)
    assert list(f) == [112.0, 113.0, 114.0]  # months 12, 13, 14 -> targets 24, 25, 26


def test_prior_year_falls_back_when_history_is_short():
    h = np.array([2.0, 4.0, 6.0])
    assert (prior_year(h, 2) == [4.0, 4.0]).all()


def test_ses_constant_series_is_constant():
    assert np.allclose(ses(np.full(20, 7.0), 4), 7.0)
