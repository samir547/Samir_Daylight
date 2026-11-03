"""Client knowledge the models cannot see (promotions, launches, confirmed orders...), entered as a table.

One row = one fact. The monthly job reads the file and applies every row that is still valid, before the
month-by-month replay. Nothing here changes stored history; remove a row and the next run forgets it.

Columns (CSV, header required; unknown columns are ignored):
  marketplace   UK / US ...; blank = every marketplace
  asin          the ASIN the fact is about (required)
  month_start   first day of the month it applies to (2026-12-01); blank = every forecast month
  type          one of TYPES below (required)
  value         number, meaning depends on the type
  note          why (shown next to the number in the output)
  entered_by    who entered it
  valid_until   last day the row should still be used (blank = until removed); stops old notes lingering

Types and what `value` means:
  demand_multiplier  multiply expected sales for that month (1.5 = 50% more, 0.8 = 20% less)
  demand_override    replace expected sales for that month with this many units
  pinned_order       a confirmed order of this many units arriving that month (needs month_start)
  stock_override     Amazon's starting stock for this ASIN, in units
  open_po_override   open purchase-order units that arrive in the first forecast month
  cover_override     the months of stock Amazon reorders at
  lot_override       the usual order size, in units
  discontinue        no new orders from month_start onwards (value ignored)
  delay_orders       move this ASIN's expected orders later by `value` months
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

TYPES = {"demand_multiplier", "demand_override", "pinned_order", "stock_override", "open_po_override",
         "cover_override", "lot_override", "discontinue", "delay_orders"}
NEEDS_VALUE = TYPES - {"discontinue"}
NEEDS_MONTH = {"pinned_order"}
COLUMNS = ["marketplace", "asin", "month_start", "type", "value", "note", "entered_by", "valid_until"]


def empty() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def load_adjustments(path: Path | str | None, marketplace: str, first_forecast_month: pd.Timestamp) -> pd.DataFrame:
    """Read, validate and filter the adjustments file. A missing file means no adjustments.
    A bad row stops the run with a message naming the row, so a typo is never silently ignored."""
    if not path or not Path(path).exists():
        return empty()
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLUMNS:
        if c not in raw.columns:
            raw[c] = ""
    df = raw[COLUMNS].copy()
    df = df[df["asin"].str.strip() != ""]
    problems = []
    for i, r in df.iterrows():
        line = i + 2                                           # header is line 1
        t = r["type"].strip()
        if t not in TYPES:
            problems.append(f"line {line}: unknown type '{t}'")
            continue
        if t in NEEDS_VALUE and _num(r["value"]) is None:
            problems.append(f"line {line}: type {t} needs a numeric value")
        if t in NEEDS_MONTH and not r["month_start"].strip():
            problems.append(f"line {line}: type {t} needs month_start")
        if r["month_start"].strip() and pd.isna(pd.to_datetime(r["month_start"], errors="coerce")):
            problems.append(f"line {line}: month_start '{r['month_start']}' is not a date")
        if r["valid_until"].strip() and pd.isna(pd.to_datetime(r["valid_until"], errors="coerce")):
            problems.append(f"line {line}: valid_until '{r['valid_until']}' is not a date")
        if t in ("demand_multiplier",) and (_num(r["value"]) or 0) < 0:
            problems.append(f"line {line}: demand_multiplier cannot be negative")
    if problems:
        raise SystemExit(f"Adjustments file {path} has problems:\n  " + "\n  ".join(problems))
    df["asin"] = df["asin"].str.strip()
    df["type"] = df["type"].str.strip()
    df["value"] = df["value"].map(_num)
    df["month_start"] = pd.to_datetime(df["month_start"].replace("", pd.NA))
    df["month_start"] = df["month_start"].dt.to_period("M").dt.to_timestamp()
    df["valid_until"] = pd.to_datetime(df["valid_until"].replace("", pd.NA))
    mk = df["marketplace"].str.strip().str.upper()
    df = df[(mk == "") | (mk == marketplace.upper())]
    df = df[df["valid_until"].isna() | (df["valid_until"] >= first_forecast_month)]
    return df.reset_index(drop=True)


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


@dataclass
class AsinAdjustments:
    """Everything that applies to one ASIN, ready for the replay."""
    demand_mult: list          # per forecast month
    demand_set: list           # per forecast month, None = leave
    pinned: list               # per forecast month, units
    stock: float | None
    open_po: float | None
    cover: float | None
    lot: float | None
    stop_from: int | None      # index of first month with no new orders
    delay: int
    notes: list

    @property
    def touched(self) -> bool:
        return bool(self.notes)


def for_asin(adj: pd.DataFrame, asin: str, future: pd.DatetimeIndex) -> AsinAdjustments:
    H = len(future)
    a = AsinAdjustments([1.0] * H, [None] * H, [0.0] * H, None, None, None, None, None, 0, [])
    if adj.empty:
        return a
    rows = adj[adj["asin"] == asin]
    pos = {m: i for i, m in enumerate(future)}
    for _, r in rows.iterrows():
        t, v, m = r["type"], r["value"], r["month_start"]
        idx = list(range(H)) if pd.isna(m) else ([pos[m]] if m in pos else [])
        if t in ("demand_multiplier", "demand_override", "pinned_order", "discontinue") and not idx:
            continue                                           # month outside the horizon: nothing to apply
        if t == "demand_multiplier":
            for i in idx:
                a.demand_mult[i] *= v
        elif t == "demand_override":
            for i in idx:
                a.demand_set[i] = v
        elif t == "pinned_order":
            a.pinned[idx[0]] += v
        elif t == "stock_override":
            a.stock = v
        elif t == "open_po_override":
            a.open_po = v
        elif t == "cover_override":
            a.cover = v
        elif t == "lot_override":
            a.lot = v
        elif t == "discontinue":
            a.stop_from = min(idx) if a.stop_from is None else min(a.stop_from, min(idx))
        elif t == "delay_orders":
            a.delay = int(round(v))
        a.notes.append(_label(r))
    return a


def _label(r) -> str:
    when = "" if pd.isna(r["month_start"]) else f" {r['month_start']:%Y-%m}"
    val = "" if pd.isna(r["value"]) else f" {r['value']:g}"
    note = f" ({r['note']})" if str(r["note"]).strip() else ""
    return f"{r['type']}{when}{val}{note}"
