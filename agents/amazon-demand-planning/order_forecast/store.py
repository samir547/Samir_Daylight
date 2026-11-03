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
SQL_FILES = ("001_order_forecast_tables.sql", "004_replenish_columns.sql", "002_views.sql")   # 004 before 002: the view reads its column


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
            cur.executemany(f"""INSERT INTO dbo.amazon_order_forecast (order_forecast_run_id, asin, month_start, {cols})
                                VALUES (?, ?, ?, ?, ?{', ?' if has_chance else ''})""", rows)
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
