"""Turn a sell-out forecast into expected orders on Daylight (receipts), using Amazon's stock.

Plain-language rule, per ASIN, per forecast origin, using only data up to the origin:
  1. Start from Amazon's sellable stock at the origin and the open purchase orders (POs) already placed.
  2. Learn how many months of sales Amazon likes to hold (cover) from that ASIN's own history
     (pooled median across ASINs when the ASIN has too little history).
  3. Walk forward month by month: stock falls by forecast sales. If month-end stock would drop below
     cover x next month's forecast sales, Amazon orders the shortfall. Open POs arrive in month 1
     and cannot be cancelled, so month 1 receipts are at least the open PO quantity.
The output has the same shape as the sell-out predictions, so it is scored with the same code.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .snapshot import Snapshot

COVER_MIN, COVER_MAX = 0.5, 4.0
STALE_MONTHS = 3          # a stock reading older than this at the origin is not trusted


def _wide(inv: pd.DataFrame, col: str, months) -> pd.DataFrame:
    return inv.pivot_table(index="asin", columns="month_start", values=col, aggfunc="sum").reindex(columns=months)


def _last_known(row: np.ndarray, t_end: int) -> float:
    """Latest non-null value in the STALE_MONTHS ending at index t_end (inclusive)."""
    for k in range(t_end, max(t_end - STALE_MONTHS, -1), -1):
        if not np.isnan(row[k]):
            return float(row[k])
    return np.nan


def learn_cover(stock: np.ndarray, sales: np.ndarray, upto: int) -> float:
    s, d = stock[: upto + 1], sales[: upto + 1]
    ok = ~np.isnan(s) & ~np.isnan(d) & (d > 0)
    if ok.sum() < 3:
        return np.nan
    return float(np.clip(np.median(s[ok] / d[ok]), COVER_MIN, COVER_MAX))


def simulate(demand: np.ndarray, stock0: float, open_po: float, cover: float) -> np.ndarray:
    H = len(demand)
    rec = np.zeros(H)
    stock = stock0
    for h in range(H):
        nxt = demand[h + 1] if h + 1 < H else demand[h]
        target = cover * nxt
        inflow_known = open_po if h == 0 else 0.0
        need = max(0.0, target - (stock - demand[h] + inflow_known))
        rec[h] = inflow_known + need
        stock = stock - demand[h] + rec[h]
    return rec


def apply_order_step(pred: pd.DataFrame, snap: Snapshot, sales_wide: pd.DataFrame, suffix: str = "+orders") -> tuple[pd.DataFrame, dict]:
    months = snap.months
    pos = {m: i for i, m in enumerate(months)}
    stock_w = _wide(snap.inventory, "sellable_units", months)
    po_w = _wide(snap.inventory, "open_po_units", months)
    sales = sales_wide.reindex(columns=months)
    out, stats = [], {"asin_origins": 0, "no_stock_reading": 0, "pooled_cover": 0}
    for (method, train_end), g in pred.groupby(["method", "train_end"]):
        t = pos[train_end]
        covers = {a: learn_cover(stock_w.loc[a].to_numpy(float), sales.loc[a].to_numpy(float), t)
                  for a in g["asin"].unique() if a in stock_w.index and a in sales.index}
        pooled = np.nanmedian([c for c in covers.values()]) if covers else np.nan
        for asin, a in g.sort_values("step").groupby("asin"):
            stats["asin_origins"] += 1
            demand = a["predicted"].to_numpy(float)
            if asin not in stock_w.index:
                rec, stock0 = demand, np.nan
            else:
                stock0 = _last_known(stock_w.loc[asin].to_numpy(float), t)
                if np.isnan(stock0):
                    rec = demand
                else:
                    c = covers.get(asin, np.nan)
                    if np.isnan(c):
                        c = pooled
                        stats["pooled_cover"] += 1
                    po = _last_known(po_w.loc[asin].to_numpy(float), t)
                    rec = simulate(demand, stock0, 0.0 if np.isnan(po) else po, c)
            if asin not in stock_w.index or np.isnan(stock0):
                stats["no_stock_reading"] += 1
            b = a[["train_end", "target_month", "step", "asin"]].copy()
            b["method"] = method + suffix
            b["predicted"] = rec
            out.append(b)
    res = pd.concat(out, ignore_index=True)[["method", "train_end", "target_month", "step", "asin", "predicted"]]
    return res, stats


def fit_month1(po_w: pd.DataFrame, rec_w: pd.DataFrame, sales: pd.DataFrame, upto: int) -> tuple[float, float]:
    """Pooled least squares on months <= upto: receipts_t ~ a * open_po_(t-1) + b * sales_t, a,b >= 0."""
    X, y = [], []
    P, R, S = po_w.to_numpy(float), rec_w.reindex(po_w.index).to_numpy(float), sales.reindex(po_w.index).to_numpy(float)
    for t in range(1, upto + 1):
        ok = ~np.isnan(P[:, t - 1]) & ~np.isnan(R[:, t]) & ~np.isnan(S[:, t])
        X.append(np.c_[P[ok, t - 1], S[ok, t]]); y.append(R[ok, t])
    X, y = np.vstack(X), np.concatenate(y)
    if len(y) < 50:
        return 0.0, 1.0
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    a, b = np.clip(coef, 0.0, None)
    return float(a), float(b)


def apply_open_po(pred: pd.DataFrame, snap: Snapshot, sales_wide: pd.DataFrame, suffix: str = "+po") -> pd.DataFrame:
    """Lighter rule: month 1 uses Amazon's open POs (blended with forecast sales, weights learned
    before the origin); months 2+ keep the sales forecast. No stock top-up rule."""
    months = snap.months
    pos = {m: i for i, m in enumerate(months)}
    po_w = _wide(snap.inventory, "open_po_units", months)
    rec_w = _wide(snap.inventory, "net_received_units", months)
    sales = sales_wide.reindex(columns=months)
    out = []
    for (method, train_end), g in pred.groupby(["method", "train_end"]):
        t = pos[train_end]
        a, b = fit_month1(po_w, rec_w, sales, t)
        g = g.copy()
        g["method"] = method + suffix
        first = g["step"] == 1
        po = g.loc[first, "asin"].map(lambda x: _last_known(po_w.loc[x].to_numpy(float), t) if x in po_w.index else np.nan)
        # an open-PO reading older than the origin month is not trusted: fall back to the forecast
        fresh = g.loc[first, "asin"].map(lambda x: (x in po_w.index) and not np.isnan(po_w.loc[x].iloc[t]))
        blended = a * po.fillna(0.0) + b * g.loc[first, "predicted"]
        g.loc[first, "predicted"] = np.where(fresh & ~po.isna(), blended, g.loc[first, "predicted"])
        out.append(g)
    return pd.concat(out, ignore_index=True)
