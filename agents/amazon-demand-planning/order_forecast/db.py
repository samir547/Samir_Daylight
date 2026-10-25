"""Read-only database access. Only SELECT/WITH statements are accepted and the
connection asks for read-only intent. Run it with a read-only login where one exists.

Settings: DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD (required); DB_DRIVER, DB_PORT optional.
Pass --env-file to load them from a .env (for example the amazon-sp-api-ingest one).
The password is never printed.
"""
from __future__ import annotations

import os
from pathlib import Path

REQUIRED = ("DB_SERVER", "DB_NAME", "DB_USER", "DB_PASSWORD")


def load_env_file(path: Path | str) -> None:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def connect():
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing {', '.join(missing)}; set them or pass --env-file.")
    import pyodbc
    conn_str = (
        f"DRIVER={{{os.getenv('DB_DRIVER', 'ODBC Driver 18 for SQL Server')}}};"
        f"SERVER={os.getenv('DB_SERVER')},{os.getenv('DB_PORT', '1433')};"
        f"DATABASE={os.getenv('DB_NAME')};UID={os.getenv('DB_USER')};PWD={os.getenv('DB_PASSWORD')};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;ApplicationIntent=ReadOnly;"
        "Packet Size=4096;"   # larger TDS packets have hung on this network path (agents/product-review)
    )
    return pyodbc.connect(conn_str, timeout=30, autocommit=True)


def fetch(conn, sql: str, params=()) -> tuple[list[str], list[tuple]]:
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        raise ValueError("read-only: only SELECT/WITH statements are allowed")
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [c[0] for c in cur.description]
    rows = [tuple(r) for r in cur.fetchall()]
    return cols, rows
