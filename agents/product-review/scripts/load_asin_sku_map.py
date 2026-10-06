"""
Loads Daylight's Amazon ASIN -> SKU/Family/Market mapping into Azure SQL,
as dbo.amazon_asin_sku_map.

WHY THIS IS ITS OWN SCRIPT, NOT PART OF multi_source_sentiment_pipeline.py:
The data here is product-reference data -- "ASIN X is Daylight's product code
Y, sold in market Z" -- not review data. It changes when new products launch
on Amazon, not every time the review pipeline runs. Bundling it into the main
pipeline would mean re-touching this table on every run for no reason, and
would add another database round-trip to a pipeline that already has a known
flaky DB connection. Run this manually, only when the source spreadsheet
changes.

SOURCE FILE: Master_ASIN_List_-_UK_DE_ES_US.csv (expected at the repo root,
where you run this from, by default -- pass --csv-path to point elsewhere).
Expected columns: ASIN, Product Code, Family, Market, Product Description.

LOAD STRATEGY: full replace, every run (TRUNCATE + reload, inside one
transaction, so a failure partway through leaves the previous data intact
rather than a half-loaded table). This table has exactly one source of
truth -- the CSV -- so there's no incremental/merge logic to get wrong, and
at ~225 rows a full reload costs nothing.

WHAT THIS DELIBERATELY DOES NOT DO: it does not decide which SKU code wins
when a review's own raw model_number disagrees with this table's regional
Product Code (e.g. the UK-style D-code vs the DE/ES E-code for the same
product) -- that's a per-review resolution rule (decision #4: prefer this
table's Product Code, fall back to model_number only when this table has no
answer for that ASIN), and it belongs in the SQL view that joins this table
to the reviews table, not baked into how this table gets loaded.

USAGE:
    python -m scripts.load_asin_sku_map                  # load using the default CSV name at the repo root
    python -m scripts.load_asin_sku_map --csv-path X.csv  # load from a specific file
    python -m scripts.load_asin_sku_map --dry-run         # parse + validate + print summary, don't touch the DB

Needs the same .env as multi_source_sentiment_pipeline.py:
    DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD   (required)
    DB_DRIVER, DB_PORT                          (optional, same defaults as the main pipeline)
"""
import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("load_asin_sku_map")

TABLE_NAME = "amazon_asin_sku_map"
DEFAULT_CSV_NAME = "Master_ASIN_List_-_UK_DE_ES_US.csv"


def _load_db_config():
    load_dotenv()
    server = os.getenv("DB_SERVER")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    driver = os.getenv("DB_DRIVER", "ODBC Driver 18 for SQL Server")
    port = int(os.getenv("DB_PORT", "1433"))
    if not (server and name and user and password):
        raise RuntimeError(
            "DB_SERVER/DB_NAME/DB_USER/DB_PASSWORD must all be set (in .env) "
            "to load into the database. Use --dry-run to validate the CSV "
            "without needing a database connection."
        )
    return server, name, user, password, driver, port


def connect():
    """Same connection pattern as DatabaseWriter in multi_source_sentiment_pipeline.py,
    including the packet-size cap -- this network path has been observed to hang
    on larger negotiated packet sizes."""
    import pyodbc
    server, name, user, password, driver, port = _load_db_config()
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={server},{port};"
        f"DATABASE={name};"
        f"UID={user};PWD={password};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        "Packet Size=4096;"
    )
    conn = pyodbc.connect(conn_str, timeout=30)
    conn.timeout = 120
    return conn


def load_and_clean_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig")

    expected_cols = {"ASIN", "Product Code", "Family", "Market", "Product Description"}
    missing = expected_cols - set(df.columns)
    if missing:
        raise ValueError(f"{path} is missing expected column(s): {sorted(missing)}")

    def _clean(series):
        s = series.astype(str).str.strip()
        # NB: don't try to inject Python None here -- pandas' .where()/.mask()
        # silently store "missing" as a float NaN internally regardless of what
        # you pass in, and pyodbc then tries to bind that NaN as a SQL float,
        # which blows up against a NVARCHAR column. Leave missing values as
        # pandas' own NaN marker; the real, guaranteed-Python-None conversion
        # happens later in reload_table(), right at the point values are
        # handed to the database driver, using pd.isna() so it catches
        # whatever internal "missing" representation pandas used.
        return s.mask(s.str.lower().isin(["", "nan", "none"]))

    before = len(df)
    df["ASIN"] = _clean(df["ASIN"])
    df = df.dropna(subset=["ASIN"])
    dropped = before - len(df)
    if dropped:
        logger.warning("Dropped %d row(s) with no ASIN", dropped)

    df["ASIN"] = df["ASIN"].str.upper()
    df["Market"] = _clean(df["Market"])
    df["Market"] = df["Market"].str.upper()
    df["Product Code"] = _clean(df["Product Code"])
    df["Product Code"] = df["Product Code"].str.upper()
    df["Family"] = _clean(df["Family"])
    df["Product Description"] = _clean(df["Product Description"])

    # (ASIN, Market) is the natural key -- verified unique against the current
    # export, but re-checked here every run since a future export update could
    # break that assumption silently otherwise.
    dupes = df.duplicated(subset=["ASIN", "Market"]).sum()
    if dupes:
        logger.warning(
            "%d duplicate (ASIN, Market) row(s) found -- keeping the first "
            "occurrence of each. A clean export shouldn't have these; worth "
            "checking the source file if this number is ever non-zero.", dupes)
        df = df.drop_duplicates(subset=["ASIN", "Market"], keep="first")

    return df.reset_index(drop=True)


def print_summary(df: pd.DataFrame) -> None:
    total = len(df)
    unique_asins = df["ASIN"].nunique()
    have_code = int(df["Product Code"].notna().sum())
    have_family = int(df["Family"].notna().sum())

    logger.info("Parsed %d rows covering %d distinct ASINs", total, unique_asins)
    logger.info("  Product Code populated: %d/%d (%.1f%%)", have_code, total, 100 * have_code / total)
    logger.info("  Family populated:       %d/%d (%.1f%%)", have_family, total, 100 * have_family / total)
    logger.info("Market breakdown:\n%s",
                 df["Market"].value_counts().to_string())

    if have_code < total:
        logger.info(
            "%d row(s) have no Product Code in this file -- per decision #4, "
            "those ASINs fall back to the review's own raw model_number field "
            "(always populated, but always the UK-style code regardless of "
            "market) when the SQL view resolves a review's SKU.",
            total - have_code)


def ensure_schema(conn) -> None:
    cur = conn.cursor()
    cur.execute(f"""
IF OBJECT_ID('dbo.{TABLE_NAME}', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.{TABLE_NAME} (
        asin                 NVARCHAR(20)   NOT NULL,
        market               NVARCHAR(10)   NOT NULL,
        product_code         NVARCHAR(50)   NULL,
        family               NVARCHAR(200)  NULL,
        product_description  NVARCHAR(500)  NULL,
        loaded_at            DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_{TABLE_NAME} PRIMARY KEY (asin, market)
    );
END""")
    conn.commit()


def reload_table(conn, df: pd.DataFrame) -> None:
    # No explicit BEGIN TRANSACTION here -- pyodbc connections are
    # non-autocommit by default, so everything below is already inside one
    # real transaction that only becomes durable on conn.commit(). Adding an
    # extra literal BEGIN TRANSACTION on top of that (as an earlier version
    # of this script did) creates a mismatched nested transaction: commit()
    # only resolves pyodbc's own layer, the manually-started one underneath
    # it stays open, and it gets silently rolled back the moment the
    # connection closes -- no exception, no warning, just an empty table.
    cur = conn.cursor()
    try:
        cur.execute(f"TRUNCATE TABLE dbo.{TABLE_NAME}")
        insert_sql = f"""
INSERT INTO dbo.{TABLE_NAME} (asin, market, product_code, family, product_description)
VALUES (?, ?, ?, ?, ?)"""
        cols = ["ASIN", "Market", "Product Code", "Family", "Product Description"]
        # pd.isna() here (not a plain `is None` check) is what actually fixes
        # this -- it correctly recognizes every flavor of "missing" pandas
        # might be using internally (float NaN, pd.NA, etc.) and replaces
        # each one with the real Python None object pyodbc needs to bind a
        # SQL NULL correctly, regardless of the column's data type.
        rows = [
            tuple(None if pd.isna(v) else v for v in row)
            for row in df[cols].itertuples(index=False, name=None)
        ]
        batch_size = 100
        for i in range(0, len(rows), batch_size):
            cur.executemany(insert_sql, rows[i:i + batch_size])
        conn.commit()
        logger.info("Loaded %d rows into dbo.%s", len(rows), TABLE_NAME)
    except Exception:
        conn.rollback()
        logger.error("Load failed, rolled back -- dbo.%s left unchanged.", TABLE_NAME)
        raise


def verify_load(expected_count: int) -> None:
    """Opens a brand-new connection -- deliberately not reusing the one that
    just wrote -- and confirms the row count is actually visible from a fresh
    session. This is the check that would have caught the silent-rollback bug
    immediately instead of leaving it to a separate SSMS query to discover."""
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM dbo.{TABLE_NAME}")
        actual = cur.fetchone()[0]
    finally:
        conn.close()

    if actual != expected_count:
        raise RuntimeError(
            f"Verification failed: expected {expected_count} rows in "
            f"dbo.{TABLE_NAME} but a fresh connection sees {actual}. "
            "The write did not durably commit -- do not trust this table yet."
        )
    logger.info("Verified via a separate fresh connection: dbo.%s has %d rows.",
                TABLE_NAME, actual)


def main():
    parser = argparse.ArgumentParser(
        description="Load the Amazon ASIN->SKU/Family/Market mapping into Azure SQL.")
    parser.add_argument("--csv-path", default=DEFAULT_CSV_NAME,
                         help=f"Path to the ASIN master list CSV (default: {DEFAULT_CSV_NAME}, "
                              "expected at the repo root, i.e. the directory you run from)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse and validate the CSV, print the summary, but don't touch the database")
    args = parser.parse_args()

    path = Path(args.csv_path)
    if not path.exists():
        logger.error("CSV not found: %s", path.resolve())
        sys.exit(1)

    df = load_and_clean_csv(path)
    print_summary(df)

    if args.dry_run:
        logger.info("--dry-run set: not touching the database.")
        return

    conn = connect()
    try:
        ensure_schema(conn)
        reload_table(conn, df)
    finally:
        conn.close()

    verify_load(expected_count=len(df))
    logger.info("Done. dbo.%s is ready for the SQL view to join against.", TABLE_NAME)


if __name__ == "__main__":
    main()
