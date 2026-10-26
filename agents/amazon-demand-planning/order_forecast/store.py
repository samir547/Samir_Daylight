"""Write a forecast to the database, all or nothing. The only module that writes.

One forecast = one header row in dbo.amazon_order_forecast_run + its rows in dbo.amazon_order_forecast,
inserted in a single transaction. Running the same month again REPLACES the earlier run for the same
(marketplace, method, target, train_end), so a re-run never leaves two competing forecasts.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import db
from .live import LiveForecast

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"
SQL_FILES = ("001_order_forecast_tables.sql", "002_views.sql")


def split_batches(script: str) -> list[str]:
    """Split a T-SQL script on lines that hold only GO."""
    parts = re.split(r"^\s*GO\s*$", script, flags=re.IGNORECASE | re.MULTILINE)
    return [p.strip() for p in parts if p.strip()]


def init_db(conn=None) -> int:
    own = conn is None
    conn = conn or db.connect(readonly=False)
    n = 0
    try:
        cur = conn.cursor()
        for name in SQL_FILES:
            for batch in split_batches((SQL_DIR / name).read_text(encoding="utf-8")):
                cur.execute(batch)
                n += 1
    finally:
        if own:
            conn.close()
    return n


def load_forecast(f: LiveForecast, marketplace: str, snapshot_ref: str = "", conn=None) -> int:
    own = conn is None
    conn = conn or db.connect(readonly=False)
    try:
        conn.autocommit = False
        cur = conn.cursor()
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
        rows = [(run_id, a, m.date(), float(u)) for a, m, u in
                zip(f.table["asin"], f.table["month_start"], f.table["units_forecast"])]
        try:
            cur.fast_executemany = True
        except Exception:
            pass
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
