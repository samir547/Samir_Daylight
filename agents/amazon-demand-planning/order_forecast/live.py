"""The production forecast: use every closed month, forecast the next `horizon` months, hold nothing out.

The method and its settings come from config/forecast_settings.json so a change of method is a
settings edit, not a code change. Any method that was back-tested can be named there:
  seasonal, seasonal_half, mean12, mean6, mean3, prior_year, ses, naive_last
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest import forecast_series
from .baselines import METHODS
from .seasonal import VARIANTS, forecast_seasonal
from .series import to_wide
from .snapshot import Snapshot

SETTINGS_PATH = Path(__file__).resolve().parent.parent / "config" / "forecast_settings.json"
DEFAULTS = {"method": "seasonal_half", "open_po_tweak": True, "target": "ordered_units", "horizon": 6,
            "min_history": 12, "sanity_ratio_min": 0.5, "sanity_ratio_max": 1.6}


def load_settings(path: Path | None = None) -> dict:
    s = dict(DEFAULTS)
    p = Path(path) if path else SETTINGS_PATH
    if p.exists():
        s.update(json.loads(p.read_text(encoding="utf-8")))
    _check_method(s["method"])
    for pool, over in (s.get("pools") or {}).items():
        if "method" in over:
            _check_method(over["method"])
    return s


def _check_method(name: str) -> None:
    known = set(METHODS) | set(VARIANTS)
    if name not in known:
        raise SystemExit(f"Unknown method '{name}' in settings. Choose one of: {', '.join(sorted(known))}")


def settings_for_pool(s: dict, pool: str) -> dict:
    """The settings for one pool: the shared values, overridden by an entry under "pools" if there is one."""
    out = {k: v for k, v in s.items() if k != "pools"}
    out.update((s.get("pools") or {}).get(pool, {}))
    return out


@dataclass
class LiveForecast:
    table: pd.DataFrame            # asin, month_start, units_forecast
    method: str
    target: str
    train_end: pd.Timestamp
    horizon: int
    skipped_short: int
    sanity_ratio: float            # forecast total / (last 12 months actual scaled to the horizon)


def expected_last_closed_month(today: date | None = None) -> pd.Timestamp:
    t = pd.Timestamp(today or date.today())
    return (t.to_period("M") - 1).to_timestamp()


def data_is_current(snap: Snapshot, today: date | None = None) -> bool:
    return snap.months[-1] >= expected_last_closed_month(today)


def build_live_forecast(snap: Snapshot, settings: dict | None = None) -> LiveForecast:
    s = settings or load_settings()
    months, H, mh = snap.months, int(s["horizon"]), int(s["min_history"])
    future = pd.date_range(months[-1] + pd.offsets.MonthBegin(1), periods=H, freq="MS")
    wide = to_wide(snap.sales, "asin", s["target"], months, mask_before_first=True)
    ext = wide.reindex(columns=months.append(future))
    origin = [len(months)]
    if s["method"] in VARIANTS:
        pred = forecast_seasonal(ext, origin, H, mh, VARIANTS[s["method"]], s["method"])
    else:
        pred = forecast_series(ext, origin, H, {s["method"]: METHODS[s["method"]]}, mh)
    if s["open_po_tweak"]:
        from .order_step import apply_open_po
        pred = apply_open_po(pred, snap, wide.fillna(0.0), suffix="")
    pred["predicted"] = pred["predicted"].clip(lower=0.0).round(2)
    table = pred.rename(columns={"target_month": "month_start", "predicted": "units_forecast"})[
        ["asin", "month_start", "units_forecast"]].sort_values(["asin", "month_start"]).reset_index(drop=True)
    last12 = float(wide.iloc[:, -12:].fillna(0.0).to_numpy().sum())
    ratio = float(table["units_forecast"].sum() / (last12 * H / 12)) if last12 else float("nan")
    return LiveForecast(table, s["method"] + ("+po" if s["open_po_tweak"] else ""), s["target"], months[-1], H,
                        int(len(wide) - table["asin"].nunique()), ratio)


def sanity_check(f: LiveForecast, s: dict) -> str | None:
    """Return a reason not to load the forecast, or None. Guards an unattended run against nonsense."""
    if f.table.empty:
        return "the forecast is empty"
    if not np.isfinite(f.sanity_ratio) or not (s["sanity_ratio_min"] <= f.sanity_ratio <= s["sanity_ratio_max"]):
        return (f"forecast total is {f.sanity_ratio:.2f}x the last 12 months' pace "
                f"(allowed {s['sanity_ratio_min']} to {s['sanity_ratio_max']})")
    return None
