"""The replenishment simulation: predict what Amazon will order from Daylight, per ASIN per month.

Plain-language version (the design is in the stakeholder PDF):
  1. Learn each ASIN's habits from its own history: how many months of stock Amazon likes to hold before
     it reorders ("cover"), and its usual order size ("lot"). ASINs with few past orders lean on the
     typical values across all ASINs of the pool.
  2. Expected sales for the coming months, in three scenarios: Amazon's own weekly forecast (mean, P80,
     P90) blended with our own seasonal forecast. Where Amazon has no forecast we fall back to our own
     and scale it by the typical P80/P90 uplift seen in Amazon's forecasts.
  3. Replay month by month, per scenario: stock = stock - expected sales + orders arriving. If stock would
     fall below the reorder level (cover x next month's sales), Amazon orders enough to get back to it, and
     never less than a share (MIN_ORDER_SHARE) of its usual order size. Open purchase orders arrive in the
     first month. (Ordering a full usual-size lot every time was tried first and back-tested worse: orders
     are frequent, so full lots overshoot. See docs/replenish-backtest.md.)
  4. Output per ASIN-month: likely order (mean scenario), P80 and high case (P90), and the share of
     scenarios that place an order that month.
  5. Client knowledge (adjustments.py) is applied on the way in, so every effect is explainable.

Everything is deterministic and only reads the snapshot, so a run can be repeated exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from . import adjustments as adjmod
from .live import ROOT_DIR, LiveForecast
from .order_step import _last_known
from .seasonal import VARIANTS, forecast_seasonal
from .series import to_wide
from .snapshot import Snapshot

COVER_MIN, COVER_MAX = 0.25, 4.0
MIN_ORDER_SHARE = 0.25       # a new order is at least this share of the usual order size
EVENT_FRACTION = 0.3          # a month counts as an order when receipts are at least this share of the median order
FULL_TRUST_EVENTS = 4         # an ASIN with this many past orders uses only its own habits
DEFAULT_COVER, DEFAULT_LOT_MONTHS = 1.0, 3.0
DEFAULT_RATIOS = (1.25, 1.6)  # P80 and P90 over mean, used only when Amazon's forecast is absent altogether
AMAZON_FILE = "amazon_forecast.csv"


# ---------------------------------------------------------------- habits

@dataclass
class Habit:
    cover: float               # reorder when stock falls below cover x next month's sales
    lot: float                 # usual order size in units
    events: int                # orders seen in the history
    orders_per_year: float
    source: str                # own | blended | pooled


def _raw_habit(stock: np.ndarray, sales: np.ndarray, rec: np.ndarray, upto: int) -> dict:
    """What one ASIN's history up to index `upto` (inclusive) says about its ordering. Uses nothing later."""
    r = np.nan_to_num(rec[: upto + 1], nan=0.0)
    pos = r[r > 0]
    out = dict(events=0, cover=np.nan, lot=np.nan, per_year=0.0)
    first = np.where(~np.isnan(sales[: upto + 1]))[0]
    active = (upto - first[0] + 1) if len(first) else 0
    recent = sales[max(0, upto - 11): upto + 1]
    out["recent_sales"] = float(np.nanmean(recent)) if np.isfinite(recent).any() else np.nan
    if len(pos) == 0 or active == 0:
        return out
    idx = np.where(r >= EVENT_FRACTION * np.median(pos))[0]
    out["events"] = len(idx)
    out["lot"] = float(np.median(r[idx]))
    out["per_year"] = len(idx) / active * 12
    ratios = [stock[t - 1] / sales[t] for t in idx
              if t >= 1 and np.isfinite(stock[t - 1]) and np.isfinite(sales[t]) and sales[t] > 0]
    if len(ratios) >= 2:
        out["cover"] = float(np.clip(np.median(ratios), COVER_MIN, COVER_MAX))
    return out


def learn_habits(stock_w: pd.DataFrame, sales_w: pd.DataFrame, rec_w: pd.DataFrame, upto: int) -> dict[str, Habit]:
    S, D, R = (x.to_numpy(float) for x in (stock_w, sales_w, rec_w))
    raw = {a: _raw_habit(S[i], D[i], R[i], upto) for i, a in enumerate(sales_w.index)}
    covers = [h["cover"] for h in raw.values() if np.isfinite(h["cover"])]
    ratios = [h["lot"] / h["recent_sales"] for h in raw.values()
              if h["events"] >= 3 and np.isfinite(h["lot"]) and h["recent_sales"] and h["recent_sales"] > 0]
    pooled_cover = float(np.median(covers)) if covers else DEFAULT_COVER
    pooled_ratio = float(np.median(ratios)) if ratios else DEFAULT_LOT_MONTHS
    out = {}
    for a, h in raw.items():
        w = min(h["events"] / FULL_TRUST_EVENTS, 1.0)
        pooled_lot = pooled_ratio * (h["recent_sales"] if np.isfinite(h["recent_sales"]) else 0.0)
        own_cover = h["cover"] if np.isfinite(h["cover"]) else np.nan
        cover = pooled_cover if np.isnan(own_cover) else w * own_cover + (1 - w) * pooled_cover
        lot = pooled_lot if not np.isfinite(h["lot"]) else w * h["lot"] + (1 - w) * pooled_lot
        source = "pooled" if (h["events"] == 0 or w == 0) else ("own" if w >= 1 else "blended")
        out[a] = Habit(float(cover), float(max(lot, 0.0)), h["events"], h["per_year"], source)
    return out


def habits_table(habits: dict[str, Habit]) -> pd.DataFrame:
    return pd.DataFrame([{"asin": a, "cover_months": round(h.cover, 2), "usual_order_units": round(h.lot, 1),
                          "orders_seen": h.events, "orders_per_year": round(h.orders_per_year, 1), "habit_source": h.source}
                         for a, h in habits.items()])


# ---------------------------------------------------------------- open purchase orders

PO_SHARE_MIN, PO_SHARE_MAX = 0.2, 1.0


def learn_po_share(po_w: pd.DataFrame, rec_w: pd.DataFrame, upto: int) -> float:
    """Share of the open purchase orders seen at the end of a month that are received the next month,
    pooled over all ASINs and months up to `upto` (inclusive). Per ASIN, receipts are capped at the open
    PO so ordinary top-up receipts are not credited to it. The rest is assumed to arrive the month after."""
    P, Rr = po_w.to_numpy(float), rec_w.to_numpy(float)
    got = tot = 0.0
    for t in range(1, upto + 1):
        po = P[:, t - 1]
        ok = np.isfinite(po) & (po > 0)
        got += float(np.minimum(Rr[ok, t], po[ok]).sum())
        tot += float(po[ok].sum())
    return float(np.clip(got / tot, PO_SHARE_MIN, PO_SHARE_MAX)) if tot > 0 else 1.0


# ---------------------------------------------------------------- the replay

def replay(demand: np.ndarray, stock0: float, open_po: float, cover: float, lot: float,
           pinned: np.ndarray | None = None, stop_from: int | None = None,
           min_order_share: float = MIN_ORDER_SHARE, po_share: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Month-by-month stock replay. Returns (known inflow, new orders) per month.

    known inflow = open purchase orders (po_share of them in the first month, the rest in the second) and
    confirmed orders pinned by the client.
    A new order is placed in a month when stock after that month's sales (plus known inflow) is below
    cover x next month's sales; its size is the gap, but at least min_order_share x the usual order size."""
    H = len(demand)
    known, new = np.zeros(H), np.zeros(H)
    stock = float(stock0)
    for h in range(H):
        po_in = open_po * po_share if h == 0 else (open_po * (1 - po_share) if h == 1 else 0.0)
        known[h] = po_in + (pinned[h] if pinned is not None else 0.0)
        stock = max(stock - demand[h] + known[h], 0.0)
        nxt = demand[h + 1] if h + 1 < H else demand[h]
        gap = cover * nxt - stock
        if gap > 1e-9 and not (stop_from is not None and h >= stop_from):
            q = max(gap, min_order_share * lot)
            new[h] = q
            stock += q
    return known, new


# ---------------------------------------------------------------- demand scenarios

def amazon_monthly(weekly: pd.DataFrame, shrink: float = 1.0, min_coverage: float = 0.5) -> pd.DataFrame:
    """Amazon's latest weekly forecast -> monthly mean, P80, P90 per ASIN.

    Weeks go to the month of their Saturday. A month covered by fewer weeks than it has Saturdays (the
    forecast starts or ends mid-month) is scaled up if at least `min_coverage` of it is present.
    Summing weekly percentiles overstates a monthly percentile (weekly errors partly cancel), so
    `shrink` (1 = plain sum, the cautious default) pulls the gap above the mean towards zero."""
    w = weekly.copy()
    w["forecast_generation_date"] = pd.to_datetime(w["forecast_generation_date"])
    w = w[w["forecast_generation_date"] == w["forecast_generation_date"].max()].copy()
    w["week_end"] = pd.to_datetime(w["week_end"])
    w["month_start"] = w["week_end"].dt.to_period("M").dt.to_timestamp()
    g = w.groupby(["asin", "month_start"], as_index=False).agg(
        am_mean=("mean_units", "sum"), am_p80=("p80_units", "sum"), am_p90=("p90_units", "sum"),
        weeks=("week_end", "nunique"))
    sats = {m: len(pd.date_range(m, m + pd.offsets.MonthEnd(0), freq="W-SAT")) for m in g["month_start"].unique()}
    g["coverage"] = (g["weeks"] / g["month_start"].map(sats)).clip(upper=1.0)
    g = g[g["coverage"] >= min_coverage].copy()
    for c in ("am_mean", "am_p80", "am_p90"):
        g[c] = g[c] / g["coverage"]
    for c in ("am_p80", "am_p90"):
        g[c] = g["am_mean"] + shrink * np.maximum(g[c] - g["am_mean"], 0.0)
    return g[["asin", "month_start", "am_mean", "am_p80", "am_p90", "coverage"]].reset_index(drop=True)


def pooled_ratios(am: pd.DataFrame | None) -> tuple[float, float]:
    if am is None or am.empty:
        return DEFAULT_RATIOS
    ok = am[am["am_mean"] > 0]
    if ok.empty:
        return DEFAULT_RATIOS
    return (float(np.median(ok["am_p80"] / ok["am_mean"])), float(np.median(ok["am_p90"] / ok["am_mean"])))


def demand_scenarios(own: np.ndarray, am_rows: dict, future: pd.DatetimeIndex, w: float,
                     ratios: tuple[float, float]) -> np.ndarray:
    """(3, H): mean, P80, P90 expected sales. Months where Amazon has a forecast blend it with ours at weight w;
    other months use ours scaled by the pooled P80/P90 uplift."""
    H = len(future)
    out = np.zeros((3, H))
    for h, m in enumerate(future):
        o = max(float(own[h]), 0.0)
        a = am_rows.get(m)
        if a is not None and w > 0:
            base = w * a[0] + (1 - w) * o
            out[:, h] = (base, base + w * (a[1] - a[0]), base + w * (a[2] - a[0]))
        else:
            out[:, h] = (o, o * ratios[0], o * ratios[1])
    out[1] = np.maximum(out[1], out[0])
    out[2] = np.maximum(out[2], out[1])
    return out


# ---------------------------------------------------------------- the forecast

def _load_weekly(snap: Snapshot) -> pd.DataFrame | None:
    d = getattr(snap, "directory", None)
    if d and (Path(d) / AMAZON_FILE).exists():
        df = pd.read_csv(Path(d) / AMAZON_FILE)
        return df if len(df) else None
    return None


def _wide_keep_empty(inv: pd.DataFrame, col: str, months) -> pd.DataFrame:
    """ASIN x month matrix where an empty reading stays empty (NaN), not 0.

    order_step._wide sums with pivot_table, which turns an all-empty cell into 0 and so makes 'no reading'
    look like 'zero stock'. On the UK snapshot that changed 18% of empty stock cells into fake zeros."""
    g = inv.groupby(["asin", "month_start"])[col].sum(min_count=1).unstack("month_start")
    return g.reindex(columns=months)


def _arrays(snap: Snapshot, target: str):
    months = snap.months
    sales_w = to_wide(snap.sales, "asin", target, months, mask_before_first=True)
    rec_w = to_wide(snap.inventory, "asin", "net_received_units", months, mask_before_first=False).reindex(sales_w.index).fillna(0.0)
    stock_w = _wide_keep_empty(snap.inventory, "sellable_units", months).reindex(sales_w.index)
    po_w = _wide_keep_empty(snap.inventory, "open_po_units", months).reindex(sales_w.index)
    return months, sales_w, rec_w, stock_w, po_w


def _starting_position(stock_row: np.ndarray, po_row: np.ndarray, t: int) -> tuple[float, float]:
    stock0 = _last_known(stock_row, t)
    po = po_row[t] if np.isfinite(po_row[t]) else 0.0
    return stock0, float(po)


def build_replenish_forecast(snap: Snapshot, s: dict, weekly: pd.DataFrame | None = None,
                             adjustments: pd.DataFrame | None = None) -> LiveForecast:
    target, H, mh = s["target"], int(s["horizon"]), int(s["min_history"])
    months, sales_w, rec_w, stock_w, po_w = _arrays(snap, target)
    t = len(months) - 1
    future = pd.date_range(months[-1] + pd.offsets.MonthBegin(1), periods=H, freq="MS")
    marketplace = snap.manifest.get("marketplace", snap.pool)

    weekly = weekly if weekly is not None else _load_weekly(snap)
    w_amazon = float(s.get("amazon_weight", 0.5))
    am = amazon_monthly(weekly, float(s.get("percentile_shrink", 1.0))) if weekly is not None else None
    am_rows = {}
    if am is not None:
        for r in am.itertuples(index=False):
            am_rows.setdefault(r.asin, {})[r.month_start] = (r.am_mean, r.am_p80, r.am_p90)
    ratios = pooled_ratios(am)

    if adjustments is None:
        path = s.get("adjustments_file")
        path = (ROOT_DIR / path) if path and not Path(path).is_absolute() else path
        adjustments = adjmod.load_adjustments(path, marketplace, future[0])

    ext = sales_w.reindex(columns=months.append(future))
    shrink = VARIANTS[s.get("demand_method", "seasonal_half")]
    own = forecast_seasonal(ext, [len(months)], H, mh, shrink, "own")
    own_by = {a: g.sort_values("target_month")["predicted"].to_numpy(float) for a, g in own.groupby("asin")}

    habits = learn_habits(stock_w, sales_w, rec_w, t)
    min_share = float(s.get("min_order_share", MIN_ORDER_SHARE))
    po_share = learn_po_share(po_w, rec_w, t)
    stats = {"replayed": 0, "no_stock_reading": 0, "with_adjustments": 0, "amazon_months": 0}

    def run(adj_df: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for asin, o in own_by.items():
            ad = adjmod.for_asin(adj_df, asin, future)
            dem = demand_scenarios(o, am_rows.get(asin, {}), future, w_amazon, ratios)
            for h in range(H):
                if ad.demand_set[h] is not None:
                    dem[:, h] = (ad.demand_set[h], ad.demand_set[h] * ratios[0], ad.demand_set[h] * ratios[1])
                dem[:, h] *= ad.demand_mult[h]
            hab = habits[asin]
            cover = ad.cover if ad.cover is not None else hab.cover
            lot = ad.lot if ad.lot is not None else hab.lot
            stock0, po = _starting_position(stock_w.loc[asin].to_numpy(float), po_w.loc[asin].to_numpy(float), t)
            stock0 = ad.stock if ad.stock is not None else stock0
            po = ad.open_po if ad.open_po is not None else po
            pinned = np.array(ad.pinned)
            if np.isnan(stock0):
                basis = "sales_only"          # no usable stock reading: orders are assumed to follow sales
                tot = [np.maximum(dem[k], 0.0) + pinned for k in range(3)]
                if ad.stop_from is not None:
                    for x in tot:
                        x[ad.stop_from:] = pinned[ad.stop_from:]
            else:
                basis = "replay"
                tot = []
                for k in range(3):
                    known, new = replay(dem[k], stock0, po, cover, lot, pinned, ad.stop_from, min_share, po_share)
                    if ad.delay > 0:
                        new = np.r_[np.zeros(min(ad.delay, H)), new[: max(H - ad.delay, 0)]]
                    tot.append(known + new)
            chance = np.mean([x > 1e-9 for x in tot], axis=0) if basis == "replay" else np.full(H, np.nan)
            for h in range(H):
                rows.append((asin, future[h], float(tot[0][h]), float(tot[1][h]), float(tot[2][h]), float(chance[h]),
                             basis, "; ".join(ad.notes)))
        return pd.DataFrame(rows, columns=["asin", "month_start", "units_forecast", "units_p80", "units_upper",
                                           "order_chance", "basis", "adjustments"])

    table = run(adjustments)
    unadjusted = run(adjmod.empty()) if not adjustments.empty else None
    per_asin = table.groupby("asin")["basis"].first()
    stats["replayed"] = int((per_asin == "replay").sum())
    stats["no_stock_reading"] = int((per_asin == "sales_only").sum())
    stats["with_adjustments"] = int(table.loc[table["adjustments"] != "", "asin"].nunique())
    stats["amazon_months"] = int(sum(len(v) for a, v in am_rows.items() if a in own_by))
    for c in ("units_forecast", "units_p80", "units_upper"):
        table[c] = table[c].clip(lower=0.0).round(2)
    table["order_chance"] = table["order_chance"].round(2)
    table = table.sort_values(["asin", "month_start"]).reset_index(drop=True)
    last12 = float(sales_w.iloc[:, -12:].fillna(0.0).to_numpy().sum())
    ratio = float(table["units_forecast"].sum() / (last12 * H / 12)) if last12 else float("nan")
    f = LiveForecast(table, "replenish", target, months[-1], H, int(len(sales_w) - len(own_by)), ratio)
    f.extras = {"habits": habits_table({a: habits[a] for a in own_by}), "stats": stats,
                "scenario_ratios": ratios, "amazon_used": am is not None, "po_share": po_share}
    if unadjusted is not None:
        for c in ("units_forecast", "units_p80", "units_upper"):
            unadjusted[c] = unadjusted[c].clip(lower=0.0).round(2)
        unadjusted["order_chance"] = unadjusted["order_chance"].round(2)
        f.extras["unadjusted"] = replace(f, table=unadjusted.sort_values(["asin", "month_start"]).reset_index(drop=True),
                                         method="replenish_unadjusted", extras={})
    return f


# ---------------------------------------------------------------- back-test hook

def backtest_predictions(snap: Snapshot, target: str, origins: list[int], horizon: int, min_history: int,
                         demand_shrink: float = 0.5, use_po_share: bool = True, method: str = "replenish") -> pd.DataFrame:
    """The replay at past origins, using only data before each origin, in the back-test's prediction format.

    Amazon's forecast has no stored history, so demand here is our own seasonal forecast. This scores the
    reorder logic (habits, stock, open orders); Amazon's forecast cannot be back-tested yet."""
    months, sales_w, rec_w, stock_w, po_w = _arrays(snap, target)
    own = forecast_seasonal(sales_w, origins, horizon, min_history, demand_shrink, "own")
    out = []
    for t in origins:
        habits = learn_habits(stock_w, sales_w, rec_w, t - 1)
        po_share = learn_po_share(po_w, rec_w, t - 1) if use_po_share else 1.0
        g_t = own[own["train_end"] == months[t - 1]]
        for asin, g in g_t.groupby("asin"):
            g = g.sort_values("step")
            dem = g["predicted"].to_numpy(float)
            stock0, po = _starting_position(stock_w.loc[asin].to_numpy(float), po_w.loc[asin].to_numpy(float), t - 1)
            if np.isnan(stock0):
                rec = dem
            else:
                h = habits[asin]
                known, new = replay(dem, stock0, po, h.cover, h.lot, po_share=po_share)
                rec = known + new
            b = g[["train_end", "target_month", "step", "asin"]].copy()
            b["method"] = method
            b["predicted"] = rec
            out.append(b)
    return pd.concat(out, ignore_index=True)[["method", "train_end", "target_month", "step", "asin", "predicted"]]
