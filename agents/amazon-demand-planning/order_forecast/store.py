"""Write a forecast to the database, all or nothing. The only module that writes.

One forecast = one header row in dbo.amazon_order_forecast_run + its rows in dbo.amazon_order_forecast,
inserted in a single transaction. Running the same month again REPLACES the earlier run for the same
(marketplace, method, target, train_end), so a re-run never leaves two competing forecasts.

The replenish method also stores why (sql/005): basis, reason_code, reason_detail and confidence per row, one
status row per ASIN and one summary row per run. Before writing, the loader asks INFORMATION_SCHEMA which of
those exist and writes only those, printing a WARNING for the rest. It never fails because 005 is not applied,
and it never runs DDL.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import db
from .live import LiveForecast

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SQL_FILES = ("001_order_forecast_tables.sql", "004_replenish_columns.sql", "002_views.sql")   # 004 before 002: the view reads its column
# 005 and 006 are applied by hand only: application code does not run new DDL.
VIEW_IS_NEWER_SQL = "SELECT COL_LENGTH('dbo.vw_amazon_order_forecast_sku', 'reason_code')"


def _view_is_newer(cur) -> bool:
    """True when sql/006 is applied (the view has reason_code). Read-only. If the check itself fails, False,
    which keeps the old behaviour."""
    try:
        cur.execute(VIEW_IS_NEWER_SQL)
        row = cur.fetchone()
        return bool(row) and row[0] is not None
    except Exception:
        return False


def split_batches(script: str) -> list[str]:
    """Split a T-SQL script on lines that hold only GO."""
    parts = re.split(r"^\s*GO\s*$", script, flags=re.IGNORECASE | re.MULTILINE)
    return [p.strip() for p in parts if p.strip()]


def init_db(conn=None) -> tuple[int, list[str]]:
    """Apply the SQL scripts batch by batch. Returns (batches applied, problems). A problem does not stop
    the rest, so one missing permission (for example CREATE VIEW) is reported together with the others."""
    own = conn is None
    conn = conn or db.connect(readonly=False)
    applied, problems = 0, []
    try:
        cur = conn.cursor()
        for name in SQL_FILES:
            if name == "002_views.sql" and _view_is_newer(cur):
                print("Skipped 002_views.sql: the view already has the sql/006 columns; re-applying 002 would remove them.")
                continue
            for batch in split_batches((SQL_DIR / name).read_text(encoding="utf-8")):
                try:
                    cur.execute(batch)
                    applied += 1
                except Exception as e:
                    first = next((ln.strip() for ln in batch.splitlines() if ln.strip() and not ln.strip().startswith("--")), "")
                    msg = str(e)
                    hint = ""
                    if "permission" in msg.lower() or "denied" in msg.lower():
                        hint = " (a permission is missing for the shared login: ask an admin to run this script by hand)"
                    problems.append(f"{name}: {first[:70]} -> {msg[-160:]}{hint}")
    finally:
        if own:
            conn.close()
    return applied, problems


NEW_COLUMNS = ("basis", "reason_code", "reason_detail", "confidence")
STATUS_TABLE, SUMMARY_TABLE = "amazon_order_forecast_asin_status", "amazon_order_forecast_run_summary"
SCHEMA_SQL = """SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = 'dbo' AND TABLE_NAME IN ('amazon_order_forecast', 'amazon_order_forecast_asin_status',
                                              'amazon_order_forecast_run_summary')"""
REASON_DETAIL_WIDTH = 500       # VARCHAR(500) in sql/005
STATUS_COLUMNS = ("asin", "status", "reason_code", "confidence", "basis", "habit_source", "months_of_history",
                  "has_amazon_forecast", "has_stock_data", "last12_units", "forecast_units")
SUMMARY_COLUMNS = ("asins_in_sales", "asins_forecast", "young_product_backups", "skipped_short_history", "skipped_no_sales",
                   "asins_without_amazon_forecast", "amazon_only_dropped", "inventory_only_dropped", "sales_only_asins",
                   "default_habit_asins", "seasonality_mode", "market_months", "sanity_ratio", "sanity_ratio_all_asins",
                   "skipped_sales_share", "amazon_generation_date")


@dataclass
class ExplanationSchema:
    columns: bool = False           # the four sql/005 columns on dbo.amazon_order_forecast
    status: bool = False            # dbo.amazon_order_forecast_asin_status
    summary: bool = False           # dbo.amazon_order_forecast_run_summary
    missing: list = field(default_factory=list)


def explanation_schema(cur) -> ExplanationSchema:
    """Which of the sql/005 objects exist. Read-only. If the question itself fails, none (legacy load)."""
    try:
        cur.execute(SCHEMA_SQL)
        found = {(str(tb).lower(), str(col).lower()) for tb, col in cur.fetchall()}
    except Exception as e:
        return ExplanationSchema(missing=[f"(could not read INFORMATION_SCHEMA: {type(e).__name__})"])
    tables = {tb for tb, _ in found}
    s = ExplanationSchema(columns=all(("amazon_order_forecast", c) in found for c in NEW_COLUMNS),
                          status=STATUS_TABLE in tables, summary=SUMMARY_TABLE in tables)
    if not s.columns:
        s.missing.append("columns " + ", ".join(c for c in NEW_COLUMNS if ("amazon_order_forecast", c) not in found)
                         + " on dbo.amazon_order_forecast")
    s.missing += [f"table dbo.{n}" for n, ok in ((STATUS_TABLE, s.status), (SUMMARY_TABLE, s.summary)) if not ok]
    return s


def _plain(v):
    """A value pyodbc can bind: numpy to Python, NaN to None."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _trim_codes(detail, width: int = REASON_DETAIL_WIDTH):
    """reason_detail cut at a code boundary if it would not fit (it never should: see test_store_schema)."""
    if detail is None or len(detail) <= width:
        return detail
    return detail[: width].rsplit("|", 1)[0]


def _ratio(v):
    v = _plain(v)
    return None if v is None or abs(v) >= 1e6 else v          # DECIMAL(9,3)


def _status_rows(run_id: int, decisions) -> tuple[list, list]:
    rows, bad = [], []
    for r in decisions.itertuples(index=False):
        a = str(r.asin).strip()
        if len(a) != 10:                       # the column is VARCHAR(10) like every other asin column
            bad.append(a)
            continue
        vals = [_plain(getattr(r, c)) for c in STATUS_COLUMNS[1:]]
        rows.append((run_id, a, *vals))
    return rows, bad


def _summary_row(run_id: int, s: dict) -> tuple:
    vals = []
    for c in SUMMARY_COLUMNS:
        v = s.get(c)
        if c in ("sanity_ratio", "sanity_ratio_all_asins"):
            v = _ratio(v)
        elif c == "amazon_generation_date" and v:
            v = date.fromisoformat(str(v)[:10])
        vals.append(_plain(v))
    return (run_id, *vals, json.dumps(s))


def load_forecast(f: LiveForecast, marketplace: str, snapshot_ref: str = "", conn=None) -> int:
    own = conn is None
    conn = conn or db.connect(readonly=False)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        replenish = "units_upper" in f.table.columns
        key = (marketplace, f.method, f.target, f.train_end.date())
        schema = explanation_schema(cur) if replenish else ExplanationSchema()
        if replenish and schema.missing:
            print(f"WARNING: not stored for this run: {'; '.join(schema.missing)}. The forecast itself loads. "
                  f"Apply sql/005_run_explanations.sql (and sql/006_view_explanations.sql) by hand to store them.")
        for table, ok in ((STATUS_TABLE, schema.status), (SUMMARY_TABLE, schema.summary)):
            if ok:                             # same scope as the forecast rows below: this run's key only
                cur.execute(f"""DELETE x FROM dbo.{table} x
                               JOIN dbo.amazon_order_forecast_run r ON r.order_forecast_run_id = x.order_forecast_run_id
                               WHERE r.marketplace = ? AND r.method = ? AND r.target_measure = ? AND r.train_end = ?""", key)
        cur.execute("""DELETE f FROM dbo.amazon_order_forecast f
                       JOIN dbo.amazon_order_forecast_run r ON r.order_forecast_run_id = f.order_forecast_run_id
                       WHERE r.marketplace = ? AND r.method = ? AND r.target_measure = ? AND r.train_end = ?""",
                    (marketplace, f.method, f.target, f.train_end.date()))
        cur.execute("""DELETE FROM dbo.amazon_order_forecast_run
                       WHERE marketplace = ? AND method = ? AND target_measure = ? AND train_end = ?""",
                    (marketplace, f.method, f.target, f.train_end.date()))
        cur.execute("""INSERT INTO dbo.amazon_order_forecast_run
                       (marketplace, method, target_measure, train_end, horizon_months, snapshot_ref)
                       OUTPUT INSERTED.order_forecast_run_id VALUES (?, ?, ?, ?, ?, ?)""",
                    (marketplace, f.method, f.target, f.train_end.date(), f.horizon, snapshot_ref or None))
        run_id = int(cur.fetchone()[0])
        try:
            cur.fast_executemany = True
        except Exception:
            pass
        if "units_upper" in f.table.columns:           # the replenish method: likely case, high case, chance of order
            cur.execute("SELECT COL_LENGTH('dbo.amazon_order_forecast', 'order_chance')")
            has_chance = cur.fetchone()[0] is not None
            t = f.table
            chance = [None if v != v else float(v) for v in t["order_chance"]]
            rows = [(run_id, a, m.date(), float(u), float(hi)) + ((c,) if has_chance else ())
                    for a, m, u, hi, c in zip(t["asin"], t["month_start"], t["units_forecast"], t["units_upper"], chance)]
            cols = "units_forecast, units_upper" + (", order_chance" if has_chance else "")
            explain = schema.columns and all(c in t.columns for c in NEW_COLUMNS)
            if explain:
                extra = [(_plain(b), _plain(rc), _trim_codes(_plain(rd)), _plain(cf)) for b, rc, rd, cf in
                         zip(t["basis"], t["reason_code"], t["reason_detail"], t["confidence"])]
                rows = [r + e for r, e in zip(rows, extra)]
                cols += ", " + ", ".join(NEW_COLUMNS)
            marks = ", ".join("?" * (3 + cols.count(",") + 1))
            if rows:
                cur.executemany(f"""INSERT INTO dbo.amazon_order_forecast (order_forecast_run_id, asin, month_start, {cols})
                                    VALUES ({marks})""", rows)
            decisions, summary = f.extras.get("decisions"), f.extras.get("summary")
            if schema.status and decisions is not None and len(decisions):
                srows, bad = _status_rows(run_id, decisions)
                if bad:
                    print(f"WARNING: {len(bad)} ASIN(s) not 10 characters, kept out of {STATUS_TABLE}: {', '.join(bad[:10])}")
                if srows:
                    cur.executemany(f"""INSERT INTO dbo.{STATUS_TABLE} (order_forecast_run_id, {', '.join(STATUS_COLUMNS)})
                                        VALUES ({', '.join('?' * (len(STATUS_COLUMNS) + 1))})""", srows)
            if schema.summary and summary:
                cur.execute(f"""INSERT INTO dbo.{SUMMARY_TABLE} (order_forecast_run_id, {', '.join(SUMMARY_COLUMNS)}, summary_json)
                                VALUES ({', '.join('?' * (len(SUMMARY_COLUMNS) + 2))})""", _summary_row(run_id, summary))
        else:
            rows = [(run_id, a, m.date(), float(u)) for a, m, u in
                    zip(f.table["asin"], f.table["month_start"], f.table["units_forecast"])]
            cur.executemany("""INSERT INTO dbo.amazon_order_forecast (order_forecast_run_id, asin, month_start, units_forecast)
                               VALUES (?, ?, ?, ?)""", rows)
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        if own:
            conn.close()
