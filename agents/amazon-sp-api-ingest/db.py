"""
Database access for the Amazon ingest (data-ingestion-spec.md sections 7.5, 8
and 11.1): connection, run log, the sales/inventory MERGE loader, the forecast
version loader, and the data quality checks.

Rules this module keeps:
  - No DDL on permanent objects. The tables and views come from sql/001-005,
    run by hand. require_objects() fails clearly if any is missing. The only
    CREATE here is the per-load temp table #stage (section 7.5).
  - Never deletes fact rows. The one DELETE is the forecast version being
    replaced under the same forecast_run_id (D5, section 8.2).
  - A row is updated only when one of its values changed (EXISTS ... EXCEPT,
    which treats NULLs as equal).
  - WEEK and MONTH rows are never combined: nothing here sums across
    period_type.
  - The caller owns the transaction: one per marketplace. The run-log helpers
    commit on their own, so call them outside a load transaction.

Connection settings (the demand-planning agent's names): DB_SERVER, DB_NAME,
DB_USER, DB_PASSWORD (required), DB_DRIVER, DB_PORT (optional).
Tuning: DB_BATCH_ROWS (rows per executemany batch into #stage, default 500),
DB_FAST_EXECUTEMANY (1/0, default 1). All batches of a marketplace are in
one transaction.
"""
from __future__ import annotations

import os
import socket
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from sp_api_common import EXPECTED_CURRENCY  # also loads .env
from week_plan import MONTH_SAFETY_DAY

REQUIRED_ENV = ("DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD")
MESSAGE_MAX = 2000           # amazon_ingest_run.message is NVARCHAR(2000)
MIN_MONTHS = 36              # section 6.6 / post-load coverage check
STALE_WEEK_DAYS = 13         # section 6.3
STALE_FORECAST_DAYS = 9      # section 8.4
EXPECTED_FORECAST_WEEKS = 48

# Column -> SQL type of the #stage table, in table order. Mirrors sql/002 and 003.
KEY_COLS = ["marketplace", "asin", "period_type", "period_start"]
VENDOR_TABLES = {
    "sales": {
        "table": "dbo.amazon_vendor_sales",
        "columns": {
            "marketplace": "CHAR(2) NOT NULL", "asin": "VARCHAR(10) NOT NULL",
            "period_type": "VARCHAR(10) NOT NULL", "period_start": "DATE NOT NULL",
            "period_end": "DATE NOT NULL",
            "ordered_revenue": "DECIMAL(18,2) NULL", "ordered_units": "INT NULL",
            "shipped_revenue": "DECIMAL(18,2) NULL", "shipped_cogs": "DECIMAL(18,2) NULL",
            "shipped_units": "INT NULL", "customer_returns": "INT NULL", "currency": "CHAR(3) NULL",
        },
    },
    "inventory": {
        "table": "dbo.amazon_vendor_inventory",
        "columns": {
            "marketplace": "CHAR(2) NOT NULL", "asin": "VARCHAR(10) NOT NULL",
            "period_type": "VARCHAR(10) NOT NULL", "period_start": "DATE NOT NULL",
            "period_end": "DATE NOT NULL",
            "net_received_units": "INT NULL", "net_received_cost": "DECIMAL(18,2) NULL",
            "open_po_units": "INT NULL", "sellable_units": "INT NULL", "sellable_cost": "DECIMAL(18,2) NULL",
            "unsellable_units": "INT NULL", "unsellable_cost": "DECIMAL(18,2) NULL",
            "sell_through_rate": "DECIMAL(9,4) NULL", "aged_90plus_units": "INT NULL",
            "aged_90plus_cost": "DECIMAL(18,2) NULL", "unhealthy_units": "INT NULL",
            "unhealthy_cost": "DECIMAL(18,2) NULL", "currency": "CHAR(3) NULL",
        },
    },
}
REQUIRED_OBJECTS = ("dbo.amazon_ingest_run", "dbo.amazon_vendor_sales", "dbo.amazon_vendor_inventory",
                    "dbo.amazon_forecast_run", "dbo.amazon_forecast_weekly")


class DataError(ValueError):
    """Rows that cannot be loaded as they are (bad dates, duplicate keys, bad numbers)."""


# ---------------------------------------------------------------- connection

def missing_settings() -> list[str]:
    return [v for v in REQUIRED_ENV if not os.getenv(v)]


def connect():
    """pyodbc connection, autocommit off. Settings from the environment (.env in
    this folder is loaded by sp_api_common on import). Never prints the password."""
    missing = missing_settings()
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)} in .env - see .env.example.")
    import pyodbc
    conn_str = (
        f"DRIVER={{{os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={os.getenv('DB_SERVER')},{os.getenv('DB_PORT', '1433')};"
        f"DATABASE={os.getenv('DB_NAME')};"
        f"UID={os.getenv('DB_USER')};PWD={os.getenv('DB_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        # Larger TDS packets have hung on this network path (agents/product-review).
        "Packet Size=4096;"
    )
    for attempt in range(1, 4):
        try:
            conn = pyodbc.connect(conn_str, timeout=30, autocommit=False)
            break
        except pyodbc.OperationalError as e:
            if attempt == 3:
                raise
            print(f"    database login failed ({type(e).__name__}); retrying in {15 * attempt}s")
            time.sleep(15 * attempt)
    conn.timeout = 300  # seconds per statement, so a stalled link raises instead of hanging
    return conn


def require_objects(conn, names=REQUIRED_OBJECTS) -> None:
    """Fail clearly if a table from sql/001-004 is missing (replaces ensure_table)."""
    cur = conn.cursor()
    missing = [n for n in names if cur.execute("SELECT OBJECT_ID(?)", n).fetchone()[0] is None]
    conn.rollback()
    if missing:
        raise RuntimeError(f"Missing database objects: {', '.join(missing)}. Apply sql/001-005 by hand first "
                           f"(data-ingestion-spec.md section 7.3).")


# ---------------------------------------------------------------- run log

def start_run(conn, job_name: str, marketplace: str, trigger_type: str,
              window_start: date | None = None, window_end: date | None = None) -> int:
    cur = conn.cursor()
    run_id = cur.execute(
        "SET NOCOUNT ON; INSERT dbo.amazon_ingest_run (job_name, marketplace, trigger_type, window_start, "
        "window_end, status, host_name) OUTPUT inserted.run_id VALUES (?, ?, ?, ?, ?, 'running', ?);",
        job_name, marketplace, trigger_type, window_start, window_end, socket.gethostname()[:128],
    ).fetchone()[0]
    conn.commit()
    return int(run_id)


def finish_run(conn, run_id: int, status: str, rows_read: int | None = None, rows_inserted: int | None = None,
               rows_updated: int | None = None, message: str | None = None) -> None:
    if message and len(message) > MESSAGE_MAX:
        message = message[:MESSAGE_MAX - 3] + "..."
    conn.cursor().execute(
        "UPDATE dbo.amazon_ingest_run SET status = ?, rows_read = ?, rows_inserted = ?, rows_updated = ?, "
        "message = ?, finished_at = SYSUTCDATETIME() WHERE run_id = ?;",
        status, rows_read, rows_inserted, rows_updated, message, run_id)
    conn.commit()


# ---------------------------------------------------------------- row preparation

def _num(value, kind: str, col: str):
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        raise DataError(f"{col}: not a number: {value!r}")
    if not d.is_finite():
        raise DataError(f"{col}: not a finite number: {value!r}")
    if kind == "INT":
        if d != d.to_integral_value():
            raise DataError(f"{col}: expected a whole number, got {value!r}")
        return int(d)
    return d


def _date(value, col: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        raise DataError(f"{col}: not an ISO date: {value!r}")


def check_period(period_type: str, start: date, end: date) -> str | None:
    """None if the dates fit the grain, else why not. MONTH: whole calendar month.
    WEEK: Sunday to Saturday."""
    if period_type == "MONTH":
        nxt = (start.replace(day=1) + timedelta(days=32)).replace(day=1)
        if start.day != 1 or end != nxt - timedelta(days=1):
            return f"MONTH row {start}..{end} is not a whole calendar month"
    elif period_type == "WEEK":
        if start.weekday() != 6 or end != start + timedelta(days=6):
            return f"WEEK row {start}..{end} is not Sunday..Saturday"
    else:
        return f"period_type must be WEEK or MONTH, got {period_type!r}"
    return None


def prepare_vendor_rows(kind: str, rows: list[dict], period_type: str | None = None) -> list[tuple]:
    """Parsed report or CSV rows (keys as in the CSVs: start_date/end_date) ->
    tuples in #stage column order. `period_type` applies to rows without one.
    Raises DataError on a bad value, a misaligned period or a duplicate key."""
    cols = VENDOR_TABLES[kind]["columns"]
    out, seen = [], set()
    for i, r in enumerate(rows, 1):
        pt = (r.get("period_type") or period_type or "").strip().upper()
        start = _date(r.get("period_start", r.get("start_date")), "start_date")
        end = _date(r.get("period_end", r.get("end_date")), "end_date")
        problem = check_period(pt, start, end)
        if problem:
            raise DataError(f"row {i}: {problem}")
        mp = str(r.get("marketplace") or "").strip().upper()
        asin = str(r.get("asin") or "").strip()
        if len(mp) != 2 or not asin or len(asin) > 10:
            raise DataError(f"row {i}: bad marketplace/asin {mp!r}/{asin!r}")
        key = (mp, asin, pt, start)
        if key in seen:
            raise DataError(f"row {i}: duplicate key {key}")
        seen.add(key)
        values = {"marketplace": mp, "asin": asin, "period_type": pt, "period_start": start, "period_end": end}
        for col, sql_type in cols.items():
            if col in values:
                continue
            if col == "currency":
                cur = r.get("currency")
                values[col] = str(cur).strip().upper() if cur not in (None, "") else None
            else:
                values[col] = _num(r.get(col), "INT" if sql_type.startswith("INT") else "DEC", col)
        out.append(tuple(values[c] for c in cols))
    return out


# ---------------------------------------------------------------- vendor MERGE

def _stage_and_merge(conn, kind: str, rows: list[tuple], run_id: int) -> tuple[int, int]:
    spec = VENDOR_TABLES[kind]
    cols = list(spec["columns"])
    measures = [c for c in cols if c not in KEY_COLS]          # period_end + measures + currency
    cur = conn.cursor()
    cur.execute("IF OBJECT_ID('tempdb..#stage') IS NOT NULL DROP TABLE #stage;")
    cur.execute("CREATE TABLE #stage (" + ", ".join(f"{c} {t}" for c, t in spec["columns"].items())
                + ", PRIMARY KEY (" + ", ".join(KEY_COLS) + "));")
    insert = f"INSERT #stage ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))});"
    batch = max(1, int(os.getenv("DB_BATCH_ROWS", "500")))
    ins_cur = conn.cursor()
    ins_cur.fast_executemany = os.getenv("DB_FAST_EXECUTEMANY", "1") != "0"
    for i in range(0, len(rows), batch):
        ins_cur.executemany(insert, rows[i:i + batch])
    on = " AND ".join(f"t.{c} = s.{c}" for c in KEY_COLS)
    sql = f"""
SET NOCOUNT ON;
DECLARE @actions TABLE (action NVARCHAR(10));
MERGE {spec['table']} WITH (HOLDLOCK) AS t
USING #stage AS s ON {on}
WHEN MATCHED AND EXISTS (SELECT {', '.join('t.' + c for c in measures)}
                         EXCEPT
                         SELECT {', '.join('s.' + c for c in measures)})
    THEN UPDATE SET {', '.join(f't.{c} = s.{c}' for c in measures)},
                    t.last_updated_at = SYSUTCDATETIME(), t.last_run_id = ?
WHEN NOT MATCHED BY TARGET
    THEN INSERT ({', '.join(cols)}, last_run_id) VALUES ({', '.join('s.' + c for c in cols)}, ?)
OUTPUT $action INTO @actions;
SELECT COALESCE(SUM(CASE WHEN action = 'INSERT' THEN 1 ELSE 0 END), 0),
       COALESCE(SUM(CASE WHEN action = 'UPDATE' THEN 1 ELSE 0 END), 0)
FROM @actions;"""
    inserted, updated = cur.execute(sql, run_id, run_id).fetchone()
    cur.execute("DROP TABLE #stage;")
    return int(inserted), int(updated)


def merge_vendor_rows(conn, kind: str, rows: list[tuple], run_id: int) -> tuple[int, int]:
    """Upsert prepared rows (prepare_vendor_rows) inside the caller's transaction.
    Returns (rows_inserted, rows_updated). No commit, no delete."""
    if not rows:
        return 0, 0
    return _stage_and_merge(conn, kind, rows, run_id)


def stored_week_ends(conn, kind: str, marketplace: str) -> set[date]:
    """Week-ending Saturdays already in the weekly table for one marketplace (any ASIN)."""
    cur = conn.cursor()
    rows = cur.execute(f"SELECT DISTINCT period_end FROM {VENDOR_TABLES[kind]['table']} "
                       "WHERE marketplace = ? AND period_type = 'WEEK';", marketplace).fetchall()
    conn.rollback()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- forecast versions

def _q2(v):
    return None if v is None else Decimal(str(v)).quantize(Decimal("0.01"))


def _forecast_key_rows(rows):
    return {(r["asin"], r["week_start"], r["week_end"], int(r["weeks_ahead"]),
             _q2(r["mean_units"]), _q2(r["p70_units"]), _q2(r["p80_units"]), _q2(r["p90_units"]))
            for r in rows}


def load_forecast_version(conn, marketplace: str, selling_program: str, generation_date: date,
                          report_last_updated: date | None, rows: list[dict], ingest_run_id: int,
                          raw_file_path: str | None) -> tuple[str, int]:
    """One forecast version in the caller's transaction (spec 8.2, D5). `rows` have
    asin, week_start, week_end, weeks_ahead, mean_units, p70_units, p80_units,
    p90_units. Returns (outcome, forecast_run_id): 'new' (a new generation date),
    'replaced' (same date, different content: weekly rows replaced, same id) or
    'unchanged' (same date, identical content: nothing written)."""
    asin_count = len({r["asin"] for r in rows})
    week_count = len({r["week_start"] for r in rows})
    cur = conn.cursor()
    found = cur.execute(
        "SELECT forecast_run_id FROM dbo.amazon_forecast_run WITH (UPDLOCK, HOLDLOCK) "
        "WHERE marketplace = ? AND selling_program = ? AND forecast_generation_date = ?;",
        marketplace, selling_program, generation_date).fetchone()
    if found:
        fid = int(found[0])
        stored = cur.execute(
            "SELECT asin, week_start, week_end, weeks_ahead, mean_units, p70_units, p80_units, p90_units "
            "FROM dbo.amazon_forecast_weekly WHERE forecast_run_id = ?;", fid).fetchall()
        stored_rows = [dict(zip(("asin", "week_start", "week_end", "weeks_ahead", "mean_units",
                                 "p70_units", "p80_units", "p90_units"), s)) for s in stored]
        if _forecast_key_rows(stored_rows) == _forecast_key_rows(rows):
            return "unchanged", fid
        cur.execute("DELETE dbo.amazon_forecast_weekly WHERE forecast_run_id = ?;", fid)
        cur.execute("UPDATE dbo.amazon_forecast_run SET pulled_at = SYSUTCDATETIME(), report_last_updated = ?, "
                    "asin_count = ?, row_count = ?, week_count = ?, ingest_run_id = ?, raw_file_path = ? "
                    "WHERE forecast_run_id = ?;",
                    report_last_updated, asin_count, len(rows), week_count, ingest_run_id, raw_file_path, fid)
        outcome = "replaced"
    else:
        fid = int(cur.execute(
            "SET NOCOUNT ON; INSERT dbo.amazon_forecast_run (marketplace, selling_program, forecast_generation_date, "
            "report_last_updated, asin_count, row_count, week_count, ingest_run_id, raw_file_path) "
            "OUTPUT inserted.forecast_run_id VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
            marketplace, selling_program, generation_date, report_last_updated, asin_count, len(rows),
            week_count, ingest_run_id, raw_file_path).fetchone()[0])
        outcome = "new"
    ins = conn.cursor()
    ins.fast_executemany = os.getenv("DB_FAST_EXECUTEMANY", "1") != "0"
    batch = max(1, int(os.getenv("DB_BATCH_ROWS", "500")))
    params = [(fid, r["asin"], r["week_start"], r["week_end"], int(r["weeks_ahead"]), _q2(r["mean_units"]),
               _q2(r["p70_units"]), _q2(r["p80_units"]), _q2(r["p90_units"])) for r in rows]
    for i in range(0, len(params), batch):
        ins.executemany("INSERT dbo.amazon_forecast_weekly (forecast_run_id, asin, week_start, week_end, weeks_ahead, "
                        "mean_units, p70_units, p80_units, p90_units) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                        params[i:i + batch])
    return outcome, fid


# ---------------------------------------------------------------- data quality (section 11.1)

def last_closed_month_start(today: date) -> date:
    """First day of the newest month a MONTH row can exist for (MONTH_SAFETY_DAY rule)."""
    first = today.replace(day=1)
    prev = (first - timedelta(days=1)).replace(day=1)
    return prev if today.day > MONTH_SAFETY_DAY else (prev - timedelta(days=1)).replace(day=1)


def month_coverage(conn, kind: str, today: date, marketplaces=None) -> list[dict]:
    """Per marketplace: distinct MONTH months, first and last month, and the two
    post-load flags: fewer than MIN_MONTHS months, last month before the last
    closed month. Marketplaces in `marketplaces` with no MONTH rows are listed too."""
    table = VENDOR_TABLES[kind]["table"]
    cur = conn.cursor()
    rows = cur.execute(f"SELECT marketplace, COUNT(DISTINCT period_start), MIN(period_start), MAX(period_start) "
                       f"FROM {table} WHERE period_type = 'MONTH' GROUP BY marketplace;").fetchall()
    conn.rollback()
    lcm = last_closed_month_start(today)
    found = {r[0].strip(): (int(r[1]), r[2], r[3]) for r in rows}
    out = []
    for mp in sorted(set(found) | set(marketplaces or ())):
        n, first, last = found.get(mp, (0, None, None))
        out.append({"marketplace": mp, "months": n, "first_month": first, "last_month": last,
                    "last_closed_month": lcm, "too_few_months": n < MIN_MONTHS,
                    "behind": last is None or last < lcm})
    return out


def coverage_lines(kind: str, coverage: list[dict]) -> list[str]:
    lines = []
    for c in coverage:
        flags = []
        if c["too_few_months"]:
            flags.append(f"FEWER THAN {MIN_MONTHS} MONTHS")
        if c["behind"]:
            flags.append(f"last month before last closed month {c['last_closed_month']:%Y-%m}")
        span = (f"{c['first_month']:%Y-%m}..{c['last_month']:%Y-%m}" if c["last_month"] else "no MONTH rows")
        lines.append(f"{kind} {c['marketplace']}: {c['months']} months ({span})"
                     + (f" - {'; '.join(flags)}" if flags else " - ok"))
    return lines


def freshness_alerts(conn, today: date) -> list[str]:
    """Stale weekly data (newest WEEK row older than STALE_WEEK_DAYS, section 6.3)
    and stale forecasts (latest generation date older than STALE_FORECAST_DAYS,
    section 8.4). Run at the start of every scheduled job, independent of the load."""
    cur = conn.cursor()
    alerts = []
    for kind in VENDOR_TABLES:
        for mp, newest in cur.execute(f"SELECT marketplace, MAX(period_end) FROM {VENDOR_TABLES[kind]['table']} "
                                      "WHERE period_type = 'WEEK' GROUP BY marketplace;").fetchall():
            age = (today - newest).days
            if age > STALE_WEEK_DAYS:
                alerts.append(f"STALE {kind} WEEK data {mp.strip()}: newest week ends {newest} ({age} days old)")
    for mp, newest in cur.execute("SELECT marketplace, MAX(forecast_generation_date) FROM dbo.amazon_forecast_run "
                                  "GROUP BY marketplace;").fetchall():
        age = (today - newest).days
        if age > STALE_FORECAST_DAYS:
            alerts.append(f"STALE forecast {mp.strip()}: latest generation date {newest} ({age} days old)")
    conn.rollback()
    return alerts


def quality_checks(conn, kind: str, marketplace: str, periods: set[tuple[str, date]], today: date) -> list[str]:
    """Checks after a load, for the (period_type, period_start) pairs just loaded.
    Findings only; they never block or undo the load (section 11.1)."""
    table = VENDOR_TABLES[kind]["table"]
    cur = conn.cursor()
    out = []
    if not periods:
        return out
    pairs = sorted(periods)
    values = ", ".join("(?, ?)" for _ in pairs)
    params = [x for p in pairs for x in p]
    loaded = (f"JOIN (VALUES {values}) AS p (period_type, period_start) "
              f"ON p.period_type = t.period_type AND p.period_start = t.period_start")

    # Coverage: an ALL row for every loaded period.
    rows = cur.execute(f"SELECT p.period_type, p.period_start FROM (VALUES {values}) AS p (period_type, period_start) "
                       f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE t.marketplace = ? AND t.asin = 'ALL' "
                       f"AND t.period_type = p.period_type AND t.period_start = p.period_start);",
                       *params, marketplace).fetchall()
    if rows:
        out.append("no ALL row for " + ", ".join(f"{r[0]} {r[1]}" for r in rows[:10])
                   + (f" (+{len(rows) - 10} more)" if len(rows) > 10 else ""))

    # Currency per marketplace.
    expected = EXPECTED_CURRENCY[marketplace]
    bad = cur.execute(f"SELECT t.currency, COUNT(*) FROM {table} t {loaded} WHERE t.marketplace = ? "
                      f"AND t.currency IS NOT NULL AND t.currency <> ? GROUP BY t.currency;",
                      *params, marketplace, expected).fetchall()
    if bad:
        out.append("unexpected currency " + ", ".join(f"{c} x{n}" for c, n in bad) + f" (expected {expected})")

    # Unmapped ASINs (and whether the marketplace is in the SKU map at all).
    if cur.execute("SELECT OBJECT_ID('dbo.amazon_asin_sku_map')").fetchone()[0] is not None:
        mapped = cur.execute("SELECT COUNT(*) FROM dbo.amazon_asin_sku_map WHERE market = ?;",
                             marketplace).fetchone()[0]
        if not mapped:
            out.append(f"no {marketplace} rows in amazon_asin_sku_map: every ASIN is unmapped")
        units = "SUM(t.shipped_units)" if kind == "sales" else "NULL"
        n, u = cur.execute(f"SELECT COUNT(DISTINCT t.asin), {units} FROM {table} t {loaded} "
                           f"LEFT JOIN dbo.amazon_asin_sku_map m ON m.asin = t.asin AND m.market = t.marketplace "
                           f"WHERE t.marketplace = ? AND t.asin <> 'ALL' AND m.asin IS NULL;",
                           *params, marketplace).fetchone()
        if n:
            out.append(f"{n} unmapped ASIN(s)" + (f", {u} shipped units over the loaded periods" if u is not None
                                                  else ""))

    if kind == "sales":
        # Per-ASIN sum vs ALL row (informational).
        diffs = cur.execute(
            f"SELECT a.period_type, a.period_start, a.shipped_units, SUM(t.shipped_units) FROM {table} a "
            f"JOIN {table} t ON t.marketplace = a.marketplace AND t.period_type = a.period_type "
            f"AND t.period_start = a.period_start AND t.asin <> 'ALL' "
            f"JOIN (VALUES {values}) AS p (period_type, period_start) "
            f"ON p.period_type = a.period_type AND p.period_start = a.period_start "
            f"WHERE a.marketplace = ? AND a.asin = 'ALL' "
            f"GROUP BY a.period_type, a.period_start, a.shipped_units "
            f"HAVING SUM(t.shipped_units) <> a.shipped_units;", *params, marketplace).fetchall()
        if diffs:
            out.append(f"info: per-ASIN shipped_units differ from the ALL row in {len(diffs)} period(s)")

        # Weekly vs monthly for loaded MONTH rows (informational; never sum the two, D9).
        for pt, start in pairs:
            if pt != "MONTH":
                continue
            r = cur.execute(
                f"SELECT m.shipped_units, SUM(w.shipped_units), COUNT(w.period_start) FROM {table} m "
                f"LEFT JOIN {table} w ON w.marketplace = m.marketplace AND w.asin = 'ALL' AND w.period_type = 'WEEK' "
                f"AND w.period_end BETWEEN m.period_start AND m.period_end "
                f"WHERE m.marketplace = ? AND m.asin = 'ALL' AND m.period_type = 'MONTH' AND m.period_start = ? "
                f"GROUP BY m.shipped_units;", marketplace, start).fetchone()
            if r and r[2] and r[0]:
                out.append(f"info: {start:%Y-%m} weeks vs month shipped_units {r[1]} vs {r[0]} "
                           f"({(r[1] - r[0]) * 100.0 / r[0]:+.1f}%, {r[2]} weeks)")
    else:
        # NULL profile per period type (NULL = not supplied by Amazon).
        fields = [c for c in VENDOR_TABLES["inventory"]["columns"]
                  if c not in KEY_COLS + ["period_end", "currency"]]
        sums = ", ".join(f"SUM(CASE WHEN t.{c} IS NULL THEN 1 ELSE 0 END)" for c in fields)
        for row in cur.execute(f"SELECT t.period_type, COUNT(*), {sums} FROM {table} t {loaded} "
                               f"WHERE t.marketplace = ? GROUP BY t.period_type;", *params, marketplace).fetchall():
            nulls = [f"{c} {n}" for c, n in zip(fields, row[2:]) if n]
            if nulls:
                out.append(f"NULLs in {row[1]} {row[0]} rows: " + ", ".join(nulls))

    # loaded_through per grain.
    lt = cur.execute(f"SELECT period_type, MAX(period_end) FROM {table} WHERE marketplace = ? "
                     f"GROUP BY period_type ORDER BY period_type;", marketplace).fetchall()
    out.append("loaded_through " + ", ".join(f"{pt} {d}" for pt, d in lt))

    # Month coverage (at least MIN_MONTHS months; last month vs last closed month).
    out += [line.split(": ", 1)[1] for line in coverage_lines(kind, month_coverage(conn, kind, today, [marketplace]))
            if line.startswith(f"{kind} {marketplace}:")]
    conn.rollback()
    return out
