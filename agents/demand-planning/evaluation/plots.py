"""
Visualization. Reuses the Prophet models fitted during the forecast run
instead of refitting per series.
"""
from __future__ import annotations

import logging
from pathlib import Path

import matplotlib
matplotlib.use("Agg")   # non-interactive backend (safe for headless runs)
import matplotlib.pyplot as plt
import pandas as pd

import config

logger = logging.getLogger(__name__)


def _label(keys: tuple) -> str:
    return "_".join(str(k).replace(" ", "-").replace("/", "-") for k in keys)


def save_component_plots(models: dict, plots_dir: Path) -> None:
    """
    Save a Prophet component plot per fitted model.

    `models` maps a group-key tuple (matching config.GROUP_COLS) to a fitted
    Prophet model returned by models.prophet_model.run_forecasts.
    """
    if not models:
        logger.warning("No fitted models available for plotting.")
        return

    plots_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Generating %s component plot(s) → %s/", f"{len(models):,}", plots_dir)

    n_months = (
        (pd.Timestamp(config.FORECAST_END + "-01").to_period("M")
         - pd.Timestamp(config.TRAIN_END + "-01").to_period("M")).n
    )

    for keys, model in models.items():
        if not isinstance(keys, tuple):
            keys = (keys,)
        future = model.make_future_dataframe(periods=n_months, freq="MS")
        fcst = model.predict(future)
        fig = model.plot_components(fcst)
        fig.savefig(plots_dir / f"{_label(keys)}.png", dpi=120, bbox_inches="tight")
        plt.close(fig)

    logger.info("Plots saved.")
