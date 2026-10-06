#!/usr/bin/env python3
"""
Standalone database connectivity check for the Daylight sentiment pipeline.

Verifies, independently of daylight_sentiment/infra/db/connection.py:

  1. That pyodbc + the target ODBC driver are installed and can actually open
     a connection to DAYHANSA_SQL1 using the exact same connection-string
     shape the real pipeline uses (Encrypt=yes, TrustServerCertificate=no,
     Packet Size=4096 -- capped because larger negotiated TDS packets have
     hung indefinitely on this network path before; see connection.py and
     README Sec 7.4).
  2. That DB_USER/DB_PASSWORD authenticate and have permission to read
     schema metadata.
  3. Lists every base table in the database (schema + name + row count), so
     you can see at a glance whether dbo.sentiment_analysis_reviews /
     dbo.sentiment_analysis_runs already exist and how much is in them.

Deliberately does NOT import anything from daylight_sentiment, and never
runs ensure_schema() / CREATE TABLE / any write -- read-only, SELECT only.

Usage:
    python check_database_connection.py
    python check_database_connection.py --no-counts   # skip per-table COUNT(*)

Reads from .env (same variables connection.py reads):
    DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, DB_DRIVER (optional), DB_PORT (optional)
"""
from __future__ import annotations

import argparse
import os
import sys

import pyodbc
from dotenv import load_dotenv

DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"
DEFAULT_PORT = "1433"


def build_conn_str(driver: str, server: str, port: str, database: str,
                   user: str, password: str) -> str:
    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server},{port};"
        f"DATABASE={database};"
        f"UID={user};PWD={password};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
        "Packet Size=4096;"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-counts", action="store_true",
                        help="List tables without running COUNT(*) on each")
    args = parser.parse_args()

    load_dotenv()
    server = os.getenv("DB_SERVER")
    name = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    driver = os.getenv("DB_DRIVER", DEFAULT_DRIVER)
    port = os.getenv("DB_PORT", DEFAULT_PORT)

    missing = [n for n, v in [("DB_SERVER", server), ("DB_NAME", name),
                              ("DB_USER", user), ("DB_PASSWORD", password)]
              if not v]
    if missing:
        print(f"Missing required .env value(s): {', '.join(missing)}")
        return 1

    print(f"Driver:    {driver}")
    print(f"Server:    {server}")
    print(f"Port:      {port}")
    print(f"Database:  {name}")
    print(f"User:      {user}")
    print()

    available = list(pyodbc.drivers())
    if driver not in available:
        print(f"WARNING: {driver!r} not found in pyodbc.drivers().")
        print(f"  Drivers visible on this machine: {available or '(none)'}")
        print("  Install msodbcsql18 (Microsoft's apt repo) before this will connect.\n")

    conn_str = build_conn_str(driver, server, port, name, user, password)

    print("Connecting...")
    try:
        conn = pyodbc.connect(conn_str, timeout=30)
    except pyodbc.Error as exc:
        print(f"\nCONNECTION FAILED: {exc}")
        print("\nCommon causes:")
        print("  - ODBC driver not installed (see warning above, if any)")
        print(f"  - {server}'s firewall doesn't have this VM's public IP whitelisted")
        print("  - Wrong DB_USER / DB_PASSWORD")
        print("  - Outbound port 1433 blocked by an NSG or the VM's own firewall")
        return 1

    conn.timeout = 120  # match connection.py -- bounds a hung statement rather than blocking forever
    print("Connected.\n")

    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT @@VERSION, DB_NAME(), SUSER_SNAME(), GETUTCDATE()").fetchone()
    print(f"Server version : {row[0].splitlines()[0]}")
    print(f"Connected as   : {row[2]}")
    print(f"Current DB     : {row[1]}")
    print(f"Server UTC time: {row[3]}")
    print()

    print("Tables in this database:\n")
    tables = cursor.execute("""
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
    """).fetchall()

    if not tables:
        print("  (no tables visible -- or this user lacks permission on INFORMATION_SCHEMA.TABLES)")
    for schema, table in tables:
        label = f"{schema}.{table}"
        if args.no_counts:
            print(f"  {label}")
            continue
        try:
            count = cursor.execute(f"SELECT COUNT(*) FROM [{schema}].[{table}]").fetchone()[0]
            print(f"  {label:<50} {count:>12,} rows")
        except pyodbc.Error as exc:
            print(f"  {label:<50} (couldn't count: {exc})")

    print(f"\n{len(tables)} table(s) found.")

    print("\nPipeline tables specifically:")
    existing = {t for _s, t in tables}
    for expected in ("sentiment_analysis_reviews", "sentiment_analysis_runs"):
        status = "exists" if expected in existing else "does NOT exist yet (auto-created on first pipeline write)"
        print(f"  dbo.{expected}: {status}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
