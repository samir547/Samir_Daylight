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
  6. Young products (fewer than min_history months of sales, at least young_min_months) are not dropped:
     their expected sales are Amazon's forecast where it has the month, otherwise the average of their last
     3 months, and they go through the same habits, replay and adjustments. With no stock reading they follow
     the 3-month average only: Amazon's forecast is a demand signal and is never copied into the order forecast.
     Their rows are marked YOUNG_PRODUCT_BACKUP, confidence LOW.
  7. Every ASIN-month carries a reason_code (what decided the number), a reason_detail (every rule that
     touched it) and a confidence (HIGH / MEDIUM / LOW). The codes are listed in REASON_CODES.

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
from .seasonal import VARIANTS, forecast_seasonal, seasonal_factors
from .series import to_wide
from .snapshot import Snapshot

COVER_MIN, COVER_MAX = 0.25, 4.0
MIN_ORDER_SHARE = 0.25       # a new order is at least this share of the usual order size
EVENT_FRACTION = 0.3          # a month counts as an order when receipts are at least this share of the median order
FULL_TRUST_EVENTS = 4         # an ASIN with this many past orders uses only its own habits
DEFAULT_COVER, DEFAULT_LOT_MONTHS = 1.0, 3.0
DEFAULT_RATIOS = (1.25, 1.6)  # P80 and P90 over mean, used only when Amazon's forecast is absent altogether
AMAZON_FILE = "amazon_forecast.csv"


# ---------------------------------------------------------------- reason codes
# reason_code = the first of PRIMARY_CODES that applies to the ASIN-month (judged on the likely case, after
# rounding). reason_detail = every code that applies, primary first, then in the order of REASON_CODES.
# The codes are a stable interface (CSV, database, Power BI): add new ones, never rename.

REASON_CODES = {
    # primary: what decided the number (first match wins)
    "YOUNG_PRODUCT_BACKUP": "fewer months of sales than min_history: simple demand (Amazon's forecast, else the last "
                            "3 months' average) through the same replay; low confidence",
    "SALES_ONLY_NO_STOCK": "no stock reading in the last 3 months: orders are assumed to follow expected sales",
    "REORDER_MIN_ORDER_SIZE": "stock would fall below the reorder level; the gap was smaller than the minimum order, "
                              "so the order is min_order_share x the usual order size",
    "REORDER_TRIGGERED": "stock would fall below the reorder level (cover x next month's sales): order the gap",
    "PINNED_ORDER": "a client-confirmed order arrives this month (adjustment pinned_order); no new order needed",
    "OPEN_PO_DELIVERY": "open purchase orders arrive this month; no new order needed",
    "NO_ORDER_DELAYED": "zero: an order that was due this month was moved later (adjustment delay_orders)",
    "NO_ORDER_DISCONTINUED": "zero: an order was needed but a discontinue adjustment blocks new orders",
    "NO_ORDER_STOCK_COVERS": "zero: stock and open purchase orders cover expected sales",
    # detail only
    "DEMAND_AMAZON_BLEND": "expected sales: Amazon's forecast blended with ours (amazon_weight)",
    "DEMAND_OWN_NO_AMAZON": "expected sales: Amazon has no usable forecast for this ASIN-month, so our seasonal forecast "
                            "is used, with the pooled P80/P90 uplift",
    "DEMAND_OWN_AMAZON_OFF": "expected sales: amazon_weight is 0, so only our seasonal forecast is used",
    "DEMAND_AMAZON_FORECAST": "expected sales (young product): Amazon's forecast for the month",
    "DEMAND_RECENT_AVERAGE": "expected sales (young product): average of the last 3 months of sales",
    "DEMAND_OVERRIDE": "expected sales replaced by a client adjustment (demand_override)",
    "DEMAND_MULTIPLIER": "expected sales scaled by a client adjustment (demand_multiplier)",
    "HABIT_OWN": "reorder habits learned from this ASIN's own orders (4 or more)",
    "HABIT_BLENDED": "reorder habits partly this ASIN's own, partly the market's typical values (1 to 3 orders)",
    "HABIT_POOLED": "default reorder habits: the ASIN has no past orders, so the market's typical values are used",
    "HABIT_MARKET_DEFAULT": "the market itself has too little history, so the hard-coded defaults (cover 1 month, "
                            "order size 3 months of sales) are used",
    "HABIT_OVERRIDE": "cover or usual order size set by a client adjustment",
    "STOCK_OVERRIDE": "starting stock set by a client adjustment",
    "OPEN_PO_OVERRIDE": "open purchase orders set by a client adjustment",
    "ORDERS_DELAYED": "this ASIN's orders are moved later by a client adjustment (delay_orders)",
    "DISCONTINUED": "a client adjustment blocks new orders from this month",
    "CLIENT_ADJUSTED": "at least one client adjustment applies to this ASIN (see the adjustments column)",
    "HIGH_CASE_ORDER_ONLY": "no order in the likely case, but the high case (P80/P90) orders",
    "SEASONALITY_FLAT": "the market has under 24 months of history, so our forecast has no seasonal shape",
    "PO_SHARE_DEFAULT": "no open purchase orders in the history, so all open orders are assumed to arrive next month",
    "AMAZON_RATIOS_DEFAULT": "Amazon's forecast is absent, so the P80/P90 uplift is the default 1.25 / 1.6",
}
PRIMARY_CODES = ("YOUNG_PRODUCT_BACKUP", "SALES_ONLY_NO_STOCK", "REORDER_MIN_ORDER_SIZE", "REORDER_TRIGGERED",
                 "PINNED_ORDER", "OPEN_PO_DELIVERY", "NO_ORDER_DELAYED", "NO_ORDER_DISCONTINUED", "NO_ORDER_STOCK_COVERS")
_ORDER = {c: i for i, c in enumerate(REASON_CODES)}
LOW_CODES = {"YOUNG_PRODUCT_BACKUP", "SALES_ONLY_NO_STOCK", "HABIT_POOLED", "HABIT_MARKET_DEFAULT"}
MEDIUM_CODES = {"HABIT_BLENDED", "DEMAND_OWN_NO_AMAZON", "DEMAND_OWN_AMAZON_OFF", "SEASONALITY_FLAT"}


def confidence(codes) -> str:
    """HIGH: replayed on own habits with Amazon's forecast. MEDIUM: blended habits, no Amazon forecast that month or
    flat seasonality. LOW: young-product backup, stock unknown, or default habits. A label, not a statistic."""
    codes = set(codes)
    if codes & LOW_CODES:
        return "LOW"
    if codes & MEDIUM_CODES:
        return "MEDIUM"
    return "HIGH"


def detail(primary: str, codes) -> str:
    rest = sorted(set(codes) - {primary}, key=_ORDER.__getitem__)
    return "|".join([primary] + rest)


def replay_month_codes(units: np.ndarray, upper: np.ndarray, explain: list, delay: int) -> list:
    """(primary code, other codes) per month of a replayed ASIN, from the likely-case replay's `explain` records.

    units/upper are the stored (rounded) likely and high case. New orders were shifted `delay` months later after
    the replay, so the order that lands in month h was decided in month h - delay."""
    out = []
    for h, ex in enumerate(explain):
        src = h - delay
        lands = src >= 0 and explain[src]["ordered"]
        extra = set()
        if ex["po_in"] > 1e-9:
            extra.add("OPEN_PO_DELIVERY")
        if ex["pinned"] > 1e-9:
            extra.add("PINNED_ORDER")
        if units[h] <= 0:
            if delay > 0 and ex["ordered"]:
                primary = "NO_ORDER_DELAYED"
            elif src >= 0 and explain[src]["blocked"]:
                primary = "NO_ORDER_DISCONTINUED"
            else:
                primary = "NO_ORDER_STOCK_COVERS"
            if upper[h] > 0:
                extra.add("HIGH_CASE_ORDER_ONLY")
        elif lands:
            primary = "REORDER_MIN_ORDER_SIZE" if explain[src]["floored"] else "REORDER_TRIGGERED"
        elif ex["pinned"] > 1e-9:
            primary = "PINNED_ORDER"
        else:
            primary = "OPEN_PO_DELIVERY"
        out.append((primary, extra))
    return out


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


def learn_habits(stock_w: pd.DataFrame, sales_w: pd.DataFrame, rec_w: pd.DataFrame, upto: int,
                 info: dict | None = None) -> dict[str, Habit]:
    """Habits per ASIN. If `info` is given it is filled with what the market-level values rest on (for the
    explanations only; it changes nothing): whether the hard-coded defaults were used, and which ASINs have no
    usable own cover."""
    S, D, R = (x.to_numpy(float) for x in (stock_w, sales_w, rec_w))
    raw = {a: _raw_habit(S[i], D[i], R[i], upto) for i, a in enumerate(sales_w.index)}
    covers = [h["cover"] for h in raw.values() if np.isfinite(h["cover"])]
    ratios = [h["lot"] / h["recent_sales"] for h in raw.values()
              if h["events"] >= 3 and np.isfinite(h["lot"]) and h["recent_sales"] and h["recent_sales"] > 0]
    pooled_cover = float(np.median(covers)) if covers else DEFAULT_COVER
    pooled_ratio = float(np.median(ratios)) if ratios else DEFAULT_LOT_MONTHS
    if info is not None:
        info.update(default_cover=not covers, default_lot=not ratios, pooled_cover=pooled_cover, pooled_ratio=pooled_ratio,
                    no_own_cover={a for a, h in raw.items() if not np.isfinite(h["cover"])})
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
           min_order_share: float = MIN_ORDER_SHARE, po_share: float = 1.0,
           explain: list | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Month-by-month stock replay. Returns (known inflow, new orders) per month.

    known inflow = open purchase orders (po_share of them in the first month, the rest in the second) and
    confirmed orders pinned by the client.
    A new order is placed in a month when stock after that month's sales (plus known inflow) is below
    cover x next month's sales; its size is the gap, but at least min_order_share x the usual order size.
    If `explain` is a list, one record per month is appended to it (what arrived, whether an order was placed,
    raised to the minimum size, or blocked by a discontinue). It does not change any number."""
    H = len(demand)
    known, new = np.zeros(H), np.zeros(H)
    stock = float(stock0)
    for h in range(H):
        po_in = open_po * po_share if h == 0 else (open_po * (1 - po_share) if h == 1 else 0.0)
        known[h] = po_in + (pinned[h] if pinned is not None else 0.0)
        stock = max(stock - demand[h] + known[h], 0.0)
        nxt = demand[h + 1] if h + 1 < H else demand[h]
        gap = cover * nxt - stock
        blocked = stop_from is not None and h >= stop_from
        if gap > 1e-9 and not blocked:
            q = max(gap, min_order_share * lot)
            new[h] = q
            stock += q
        if explain is not None:
            needed = bool(gap > 1e-9)
            explain.append(dict(po_in=float(po_in), pinned=(float(pinned[h]) if pinned is not None else 0.0),
                                ordered=needed and not blocked,
                                floored=needed and not blocked and min_order_share * lot > gap,
                                blocked=needed and blocked))
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


def _stored(x: np.ndarray) -> np.ndarray:
    """The value as it is written out (clipped at 0, 2 decimals): codes are judged on what the reader sees."""
    return np.round(np.maximum(np.asarray(x, float), 0.0), 2)


def _row_codes(basis: str, tot: list, explain: list, ad, pinned: np.ndarray, dem_codes: list, hab_codes: set,
               po_default: bool) -> list:
    """(primary, all other codes) per month for one ASIN."""
    H = len(dem_codes)
    units, upper = _stored(tot[0]), _stored(tot[2])
    asin_codes = set()
    if ad.notes:
        asin_codes.add("CLIENT_ADJUSTED")
    if ad.stock is not None:
        asin_codes.add("STOCK_OVERRIDE")
    if ad.open_po is not None:
        asin_codes.add("OPEN_PO_OVERRIDE")
    if basis == "replay":
        month = replay_month_codes(units, upper, explain, ad.delay)
        if ad.delay > 0:
            asin_codes.add("ORDERS_DELAYED")
    else:
        month = []
        for h in range(H):
            extra = {"PINNED_ORDER"} if pinned[h] > 1e-9 else set()
            if units[h] <= 0 and upper[h] > 0:
                extra.add("HIGH_CASE_ORDER_ONLY")
            month.append(("SALES_ONLY_NO_STOCK", extra))
    out = []
    for h in range(H):
        primary, extra = month[h]
        rest = set(extra) | dem_codes[h] | hab_codes | asin_codes
        if ad.stop_from is not None and h >= ad.stop_from:
            rest.add("DISCONTINUED")
        if po_default and basis == "replay" and explain[h]["po_in"] > 1e-9:
            rest.add("PO_SHARE_DEFAULT")
        out.append((primary, rest - {primary}))
    return out


def sanity_ratios(sales_w: pd.DataFrame, forecast_asins: list, forecast_total: float, H: int) -> dict:
    """The sanity check compares like with like: the forecast total against the recent pace of the SAME ASINs.

    Pace per ASIN = its average monthly sales over the last 12 months it existed (a 6-month-old product counts
    6 months, not 6 plus 6 empty ones), times the horizon. For ASINs with 12+ months that is exactly the old
    "last 12 months scaled to the horizon". The old all-ASIN ratio (which also counts the sales of skipped ASINs)
    and the share of recent sales that sits in skipped ASINs are returned for the log only."""
    last12 = sales_w.iloc[:, -12:]
    fc = last12.loc[list(forecast_asins)]
    pace = float(np.nansum(np.nanmean(fc.to_numpy(float), axis=1))) * H if len(fc) else 0.0
    all12 = float(last12.fillna(0.0).to_numpy().sum())
    skipped12 = float(last12.drop(index=list(forecast_asins)).fillna(0.0).to_numpy().sum())
    return {"ratio": forecast_total / pace if pace else float("nan"),
            "ratio_all_asins": forecast_total / (all12 * H / 12) if all12 else float("nan"),
            "skipped_sales_share": skipped12 / all12 if all12 else float("nan")}


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

    hinfo: dict = {}
    habits = learn_habits(stock_w, sales_w, rec_w, t, info=hinfo)
    min_share = float(s.get("min_order_share", MIN_ORDER_SHARE))
    po_share = learn_po_share(po_w, rec_w, t)
    stats = {"replayed": 0, "no_stock_reading": 0, "with_adjustments": 0, "amazon_months": 0}

    # market-level facts, for the explanations only (none of these feeds a number)
    factors = seasonal_factors(pd.Series(np.nansum(sales_w.to_numpy(float), axis=0), index=months), shrink)
    flat_season = bool(np.allclose(factors, 1.0))
    po_hist = po_w.to_numpy(float)[:, :t]
    po_default = not bool((np.isfinite(po_hist) & (po_hist > 0)).any())
    ratios_default = am is None or am.empty or not bool((am["am_mean"] > 0).any())

    # young products: under min_history months of sales (so no seasonal forecast of their own) but at least
    # young_min_months and some sales. They are added to the loop below; nothing above changes for anyone.
    young_min = int(s.get("young_min_months", 3))
    S = sales_w.to_numpy(float)
    history = pd.Series((~np.isnan(S)).sum(axis=1), index=sales_w.index)
    sold = pd.Series(np.nansum(S, axis=1), index=sales_w.index)
    short = [a for a in sales_w.index if a not in own_by]
    young = [a for a in short if sold[a] > 0 and history[a] >= young_min]
    recent3 = {a: float(np.nanmean(S[sales_w.index.get_loc(a), -3:])) for a in young}

    def demand_codes(asin: str, ad, young_stock: bool | None = None) -> list[set]:
        """young_stock is None for a standard ASIN; for a young one, whether its stock is known."""
        a_rows = am_rows.get(asin, {})
        out = []
        for h in range(H):
            has_am = a_rows.get(future[h]) is not None and w_amazon > 0
            if young_stock is not None:
                if has_am and young_stock:
                    c = {"DEMAND_AMAZON_FORECAST"}
                else:
                    c = {"DEMAND_RECENT_AVERAGE"} | ({"AMAZON_RATIOS_DEFAULT"} if ratios_default else set())
            elif has_am:
                c = {"DEMAND_AMAZON_BLEND"}
            else:
                c = {"DEMAND_OWN_NO_AMAZON" if w_amazon > 0 else "DEMAND_OWN_AMAZON_OFF"}
                c |= {"AMAZON_RATIOS_DEFAULT"} if ratios_default else set()
            if ad.demand_set[h] is not None:
                c |= {"DEMAND_OVERRIDE"} | ({"AMAZON_RATIOS_DEFAULT"} if ratios_default else set())
            if ad.demand_mult[h] != 1.0:
                c.add("DEMAND_MULTIPLIER")
            if flat_season and young_stock is None:
                c.add("SEASONALITY_FLAT")
            out.append(c)
        return out

    def habit_codes(asin: str, ad) -> set:
        hab = habits[asin]
        c = {"HABIT_" + hab.source.upper()}
        cover_from_pool = hab.source != "own" or asin in hinfo["no_own_cover"]
        if ad.cover is None and hinfo["default_cover"] and cover_from_pool:
            c.add("HABIT_MARKET_DEFAULT")
        if ad.lot is None and hinfo["default_lot"] and hab.source != "own":
            c.add("HABIT_MARKET_DEFAULT")
        if ad.cover is not None or ad.lot is not None:
            c.add("HABIT_OVERRIDE")
        return c

    def adjust_demand(dem: np.ndarray, ad) -> np.ndarray:
        for h in range(H):
            if ad.demand_set[h] is not None:
                dem[:, h] = (ad.demand_set[h], ad.demand_set[h] * ratios[0], ad.demand_set[h] * ratios[1])
            dem[:, h] *= ad.demand_mult[h]
        return dem

    def position(asin: str, ad) -> tuple[float, float]:
        stock0, po = _starting_position(stock_w.loc[asin].to_numpy(float), po_w.loc[asin].to_numpy(float), t)
        stock0 = ad.stock if ad.stock is not None else stock0
        po = ad.open_po if ad.open_po is not None else po
        return stock0, po

    def run(adj_df: pd.DataFrame) -> pd.DataFrame:
        rows = []
        for asin in list(own_by) + young:
            ad = adjmod.for_asin(adj_df, asin, future)
            is_young = asin not in own_by
            if not is_young:
                dem = adjust_demand(demand_scenarios(own_by[asin], am_rows.get(asin, {}), future, w_amazon, ratios), ad)
                stock0, po = position(asin, ad)
            else:
                # Amazon's forecast only where the stock replay turns it into orders; with stock unknown the orders
                # would simply equal demand, so the young product follows its own recent sales instead
                stock0, po = position(asin, ad)
                am_young = am_rows.get(asin, {}) if (w_amazon > 0 and not np.isnan(stock0)) else {}
                dem = adjust_demand(demand_scenarios(np.full(H, recent3[asin]), am_young, future, 1.0, ratios), ad)
            hab = habits[asin]
            cover = ad.cover if ad.cover is not None else hab.cover
            lot = ad.lot if ad.lot is not None else hab.lot
            pinned = np.array(ad.pinned)
            explain: list = []
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
                    known, new = replay(dem[k], stock0, po, cover, lot, pinned, ad.stop_from, min_share, po_share,
                                        explain=explain if k == 0 else None)
                    if ad.delay > 0:
                        new = np.r_[np.zeros(min(ad.delay, H)), new[: max(H - ad.delay, 0)]]
                    tot.append(known + new)
            chance = np.mean([x > 1e-9 for x in tot], axis=0) if basis == "replay" else np.full(H, np.nan)
            codes = _row_codes(basis, tot, explain, ad, pinned,
                               demand_codes(asin, ad, (not np.isnan(stock0)) if is_young else None),
                               habit_codes(asin, ad) if basis == "replay" else set(), po_default)
            for h in range(H):
                primary, rest = codes[h]
                if is_young:
                    primary, rest = "YOUNG_PRODUCT_BACKUP", rest | {primary}
                rows.append((asin, future[h], float(tot[0][h]), float(tot[1][h]), float(tot[2][h]), float(chance[h]),
                             basis, "; ".join(ad.notes), primary, detail(primary, rest), confidence(rest | {primary})))
        return pd.DataFrame(rows, columns=["asin", "month_start", "units_forecast", "units_p80", "units_upper",
                                           "order_chance", "basis", "adjustments", "reason_code", "reason_detail",
                                           "confidence"])

    table = run(adjustments)
    unadjusted = run(adjmod.empty()) if not adjustments.empty else None
    per_asin = table.groupby("asin")["basis"].first()
    stats["replayed"] = int((per_asin == "replay").sum())
    stats["no_stock_reading"] = int((per_asin == "sales_only").sum())
    stats["with_adjustments"] = int(table.loc[table["adjustments"] != "", "asin"].nunique())
    forecast_asins = list(own_by) + young
    stats["amazon_months"] = int(sum(len(v) for a, v in am_rows.items() if a in set(forecast_asins)))
    for c in ("units_forecast", "units_p80", "units_upper"):
        table[c] = table[c].clip(lower=0.0).round(2)
    table["order_chance"] = table["order_chance"].round(2)
    table = table.sort_values(["asin", "month_start"]).reset_index(drop=True)
    sanity = sanity_ratios(sales_w, forecast_asins, float(table["units_forecast"].sum()), H)
    f = LiveForecast(table, "replenish", target, months[-1], H, int(len(sales_w) - len(forecast_asins)), sanity["ratio"])
    f.extras = {"habits": habits_table({a: habits[a] for a in forecast_asins}), "stats": stats,
                "scenario_ratios": ratios, "amazon_used": am is not None, "po_share": po_share, "sanity": sanity,
                "young": list(young)}
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
