#!/usr/bin/env python3
"""
Load the Amazon Vendor report CSVs in data/ into SQL Server (DAYHANSA_SQL1).

    data/daylight_sales_report*.csv     -> dbo.Daylight_sales_report
    data/daylight_inventory_report*.csv -> dbo.Daylight_inventory_report

Tables are created on first run if they don't exist. There is exactly one
row per (marketplace, asin, month): rows are upserted (MERGE) on
(marketplace, asin, start_date), so re-running the script, loading a newer
pull with overlapping months, or replacing a month-to-date row with the
final monthly figures updates rows in place and never duplicates them.
period_status says which a row is: 'final' (whole calendar month) or
'month_to_date' (start_date..end_date of a month still in progress - see
sp_api_month_to_date.py).

Usage:
    python ingest_to_db.py                 # load both report types
    python ingest_to_db.py --only sales    # or --only inventory
    python ingest_to_db.py --dry-run       # parse + validate files, no DB writes

Reads from .env: DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD,
                 DB_DRIVER (optional), DB_PORT (optional)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).parent
DATA_DIR = HERE / "data"
# One row per ASIN per month. end_date is deliberately not part of the key:
# a month-to-date row's end_date moves forward every run, and must update
# the same row rather than add a new one.
KEY_COLS = ["marketplace", "asin", "start_date"]
# Columns the loader adds on top of the CSV columns, in this order.
EXTRA_COLS = {"period_status": "NVARCHAR(20)", "source_file": "NVARCHAR(255)"}

# column -> SQL type, in CSV order. The CSV header must match exactly.
REPORTS = {
    "sales": {
        "table": "Daylight_sales_report",
        "pattern": "daylight_sales_report*.csv",
        "columns": {
            "marketplace":      "NVARCHAR(10)   NOT NULL",
            "asin":             "NVARCHAR(20)   NOT NULL",
            "start_date":       "DATE           NOT NULL",
            "end_date":         "DATE           NOT NULL",
            "ordered_revenue":  "DECIMAL(18,2)  NULL",
            "ordered_units":    "INT            NULL",
            "shipped_revenue":  "DECIMAL(18,2)  NULL",
            "shipped_cogs":     "DECIMAL(18,2)  NULL",
            "shipped_units":    "INT            NULL",
            "customer_returns": "INT            NULL",
            "currency":         "NVARCHAR(3)    NULL",
        },
    },
    "inventory": {
        "table": "Daylight_inventory_report",
        "pattern": "daylight_inventory_report*.csv",
        "columns": {
            "marketplace":        "NVARCHAR(10)   NOT NULL",
            "asin":               "NVARCHAR(20)   NOT NULL",
            "start_date":         "DATE           NOT NULL",
            "end_date":           "DATE           NOT NULL",
            "net_received_units": "INT            NULL",
            "net_received_cost":  "DECIMAL(18,2)  NULL",
            "open_po_units":      "INT            NULL",
            "sellable_units":     "INT            NULL",
            "sellable_cost":      "DECIMAL(18,2)  NULL",
            "unsellable_units":   "INT            NULL",
            "unsellable_cost":    "DECIMAL(18,2)  NULL",
            "sell_through_rate":  "DECIMAL(18,4)  NULL",
            "aged_90plus_units":  "INT            NULL",
            "aged_90plus_cost":   "DECIMAL(18,2)  NULL",
            "unhealthy_units":    "INT            NULL",
            "unhealthy_cost":     "DECIMAL(18,2)  NULL",
            "currency":           "NVARCHAR(3)    NULL",
        },
    },
}


def connect():
    load_dotenv(HERE / ".env")
    missing = [v for v in ("DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD") if not os.getenv(v)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in .env - see .env.example.")
    import pyodbc
    conn_str = (
        f"DRIVER={{{os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={os.getenv('DB_SERVER')},{os.getenv('DB_PORT', '1433')};"
        f"DATABASE={os.getenv('DB_NAME')};"
        f"UID={os.getenv('DB_USER')};PWD={os.getenv('DB_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        # Larger TDS packets have hung on this network path before
        # (see agents/product-review connection.py) - keep it capped.
        "Packet Size=4096;"
    )
    # Logins also time out now and then on this network - retry a few times.
    for attempt in range(1, 5):
        try:
            conn = pyodbc.connect(conn_str, timeout=30)
            break
        except pyodbc.OperationalError as e:
            if attempt == 4:
                raise
            print(f"    database login failed ({str(e)[:80]}...); retrying in {15 * attempt}s")
            time.sleep(15 * attempt)
    # Bound every statement so a stalled link raises instead of hanging forever.
    conn.timeout = 60
    return conn


def read_reports(spec: dict) -> pd.DataFrame:
    files = sorted(DATA_DIR.glob(spec["pattern"]))
    if not files:
        raise SystemExit(f"No files matching data/{spec['pattern']}")
    expected = list(spec["columns"])
    frames = []
    for f in files:
        df = pd.read_csv(f, dtype=str, keep_default_na=False, na_values=[""])
        if list(df.columns) != expected:
            raise SystemExit(f"{f.name}: unexpected columns {list(df.columns)}\n  expected {expected}")
        df["source_file"] = f.name
        frames.append(df)
        print(f"  read {f.name}: {len(df):,} rows")
    return prepare(pd.concat(frames, ignore_index=True), spec)


def prepare(df: pd.DataFrame, spec: dict) -> pd.DataFrame:
    """Type the columns, derive period_status, and refuse duplicate keys.
    Shared by the CSV loader and sp_api_month_to_date.py."""
    df = df.copy()
    for col in spec["columns"]:
        if col in ("marketplace", "asin", "start_date", "end_date", "currency"):
            continue
        df[col] = pd.to_numeric(df[col], errors="raise")
    for col in ("start_date", "end_date"):
        df[col] = pd.to_datetime(df[col], format="%Y-%m-%d")
    bad = df[df["start_date"].dt.day != 1]
    if not bad.empty:
        raise SystemExit(f"Rows must start on the 1st of a month:\n{bad[KEY_COLS + ['end_date']].head(10)}")
    month_end = df["start_date"] + pd.offsets.MonthEnd(0)
    df["period_status"] = (df["end_date"] == month_end).map({True: "final", False: "month_to_date"})
    for col in ("start_date", "end_date"):
        df[col] = df[col].dt.date

    dups = df[df.duplicated(KEY_COLS, keep=False)]
    if not dups.empty:
        raise SystemExit(f"Duplicate (marketplace, asin, month) rows:\n"
                         f"{dups[KEY_COLS + ['end_date', 'source_file']].head(20)}")
    return df


def to_rows(df: pd.DataFrame, columns: list[str]) -> list[list]:
    """DataFrame -> JSON-serialisable rows (NaN -> None, dates -> ISO strings)."""
    def clean(v):
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        if hasattr(v, "item"):  # numpy scalar -> python
            v = v.item()
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    return [[clean(v) for v in row] for row in df[columns].itertuples(index=False, name=None)]


def ensure_table(conn, table: str, columns: dict) -> None:
    """Create the table, or migrate one created by an earlier version of this
    script. Every step is idempotent, so this runs on every load."""
    col_defs = ",\n        ".join(f"{c} {t}" for c, t in columns.items())
    cur = conn.cursor()
    cur.execute(f"""
IF OBJECT_ID('dbo.{table}', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.{table} (
        {col_defs},
        period_status NVARCHAR(20)  NULL,
        source_file   NVARCHAR(255) NULL,
        loaded_at     DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_{table} PRIMARY KEY ({', '.join(KEY_COLS)})
    );
END""")
    # v1 tables had no period_status; every v1 row was a whole month.
    cur.execute(f"""
IF NOT EXISTS (SELECT 1 FROM sys.columns
               WHERE object_id = OBJECT_ID('dbo.{table}') AND name = 'period_status')
    ALTER TABLE dbo.{table} ADD period_status NVARCHAR(20) NULL;""")
    cur.execute(f"""
UPDATE dbo.{table}
SET period_status = CASE WHEN end_date = EOMONTH(start_date) THEN 'final' ELSE 'month_to_date' END
WHERE period_status IS NULL;""")
    # v1 keyed on (..., start_date, end_date); re-key to one row per month.
    cur.execute(f"""
IF EXISTS (SELECT 1 FROM sys.index_columns ic
           JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
           WHERE ic.object_id = OBJECT_ID('dbo.{table}')
             AND ic.index_id = INDEXPROPERTY(OBJECT_ID('dbo.{table}'), 'PK_{table}', 'IndexID')
             AND c.name = 'end_date')
BEGIN
    ALTER TABLE dbo.{table} DROP CONSTRAINT PK_{table};
    ALTER TABLE dbo.{table} ADD CONSTRAINT PK_{table} PRIMARY KEY ({', '.join(KEY_COLS)});
END""")
    conn.commit()


def merge_sql(table: str, columns: dict) -> str:
    """One MERGE that upserts a JSON array of row-arrays passed as the only parameter."""
    types = {c: t.replace("NOT NULL", "").replace("NULL", "").strip() for c, t in columns.items()}
    types.update(EXTRA_COLS)
    cols = list(types)
    with_clause = ", ".join(f"{c} {t} '$[{i}]'" for i, (c, t) in enumerate(types.items()))
    on = " AND ".join(f"t.{c} = s.{c}" for c in KEY_COLS)
    upd = ", ".join(f"t.{c} = s.{c}" for c in cols if c not in KEY_COLS)
    return f"""
SET NOCOUNT ON;
DECLARE @out TABLE (action NVARCHAR(10));
MERGE dbo.{table} AS t
USING (SELECT * FROM OPENJSON(?) WITH ({with_clause})) AS s ON {on}
WHEN MATCHED THEN UPDATE SET {upd}, t.loaded_at = SYSUTCDATETIME()
WHEN NOT MATCHED BY TARGET THEN
    INSERT ({', '.join(cols)}) VALUES ({', '.join('s.' + c for c in cols)})
OUTPUT $action INTO @out;
SELECT
    COALESCE(SUM(CASE WHEN action = 'INSERT' THEN 1 ELSE 0 END), 0),
    COALESCE(SUM(CASE WHEN action = 'UPDATE' THEN 1 ELSE 0 END), 0)
FROM @out;"""


CHUNK_ROWS = 200
MAX_SINGLE_ROW_RETRIES = 3


def load(spec: dict, df: pd.DataFrame) -> None:
    """Upsert df in chunks, each its own committed MERGE.

    The network path to this server drops the connection on certain payloads
    (bulk fast_executemany and some multi-row batches hang until the link is
    reset -- see also agents/product-review connection.py). Because every
    chunk is an idempotent MERGE, a failed chunk is simply retried on a fresh
    connection, split in half each time, down to single rows.

    The loaded data is authoritative for each (marketplace, month) it
    contains: afterwards, any row of those months that this run did not
    touch (an ASIN no longer in Amazon's report) is deleted.
    """
    import pyodbc
    table = spec["table"]
    cols = list(spec["columns"]) + list(EXTRA_COLS)
    sql = merge_sql(table, spec["columns"])
    rows = to_rows(df, cols)

    conn = connect()
    ensure_table(conn, table, spec["columns"])
    cur = conn.cursor()
    cur.execute("SELECT SYSUTCDATETIME();")
    run_started = cur.fetchone()[0]
    inserted = updated = done = 0
    pending = [(rows[i:i + CHUNK_ROWS], 0) for i in range(0, len(rows), CHUNK_ROWS)]
    pending.reverse()  # pop() from the end, keep file order
    while pending:
        batch, attempt = pending.pop()
        try:
            cur = conn.cursor()
            cur.execute(sql, json.dumps(batch))
            ins, upd = cur.fetchone()
            conn.commit()
        except pyodbc.Error as e:
            if len(batch) == 1 and attempt >= MAX_SINGLE_ROW_RETRIES:
                raise RuntimeError(f"Row {batch[0][:4]} failed {attempt + 1} times") from e
            print(f"    connection dropped on a {len(batch)}-row chunk; reconnecting and retrying smaller")
            try:
                conn.close()
            except pyodbc.Error:
                pass
            conn = connect()
            if len(batch) > 1:
                mid = len(batch) // 2
                pending += [(batch[mid:], 0), (batch[:mid], 0)]
            else:
                pending.append((batch, attempt + 1))
            continue
        inserted += ins
        updated += upd
        done += len(batch)
        if done % 2000 < len(batch) or not pending:
            print(f"    {done:,}/{len(rows):,} rows")

    months = to_rows(df[["marketplace", "start_date"]].drop_duplicates(), ["marketplace", "start_date"])
    cur = conn.cursor()
    cur.execute(f"""
SET NOCOUNT ON;
DELETE t FROM dbo.{table} t
JOIN OPENJSON(?) WITH (marketplace NVARCHAR(10) '$[0]', start_date DATE '$[1]') m
  ON t.marketplace = m.marketplace AND t.start_date = m.start_date
WHERE t.loaded_at < ?;
SELECT @@ROWCOUNT;""", json.dumps(months), run_started)
    removed = cur.fetchone()[0]
    conn.commit()

    cur.execute(f"SELECT COUNT(*) FROM dbo.{table};")
    total = cur.fetchone()[0]
    conn.close()
    print(f"  dbo.{table}: {inserted:,} inserted, {updated:,} updated, {removed:,} stale removed, "
          f"{total:,} rows total")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", choices=list(REPORTS), help="Load just one report type")
    parser.add_argument("--dry-run", action="store_true", help="Parse and validate files only")
    args = parser.parse_args()

    kinds = [args.only] if args.only else list(REPORTS)
    frames = {}
    for kind in kinds:
        print(f"[{kind}]")
        frames[kind] = read_reports(REPORTS[kind])
        print(f"  {len(frames[kind]):,} rows validated")

    if args.dry_run:
        print("Dry run - nothing written.")
        return 0

    for kind in kinds:
        print(f"[{kind}] loading ...")
        load(REPORTS[kind], frames[kind])
    return 0


if __name__ == "__main__":
    sys.exit(main())
