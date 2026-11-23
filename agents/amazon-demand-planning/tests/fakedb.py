"""An offline stand-in for a pyodbc connection, for loader and pull tests. Nothing here reaches a database.

It records every statement with its parameters, and keeps a tiny in-memory copy of the forecast tables so a
test can check what a DELETE would really remove (for example: a DE re-run must leave FR and UK alone).
`schema` says which objects "exist": the INFORMATION_SCHEMA probe and COL_LENGTH answer from it.
"""
from __future__ import annotations

import re

LEGACY = {"amazon_order_forecast": {"order_forecast_run_id", "asin", "month_start", "units_forecast", "units_lower",
                                    "units_upper", "order_chance"},
          "amazon_order_forecast_run": {"order_forecast_run_id", "marketplace", "method", "target_measure", "train_end",
                                        "horizon_months", "snapshot_ref", "created_at"}}
NEW_COLUMNS = {"reason_code", "reason_detail", "basis", "confidence"}
STATUS = {"order_forecast_run_id", "asin", "status", "reason_code", "confidence", "basis", "habit_source",
          "months_of_history", "has_amazon_forecast", "has_stock_data", "last12_units", "forecast_units"}
SUMMARY = {"order_forecast_run_id", "asins_in_sales", "summary_json"}   # enough for the probe


def schema(new_columns=False, status=False, summary=False, order_chance=True, sanity_flags=False) -> dict:
    s = {k: set(v) for k, v in LEGACY.items()}
    if not order_chance:
        s["amazon_order_forecast"].discard("order_chance")
    if new_columns:
        s["amazon_order_forecast"] |= NEW_COLUMNS
    if status:
        s["amazon_order_forecast_asin_status"] = set(STATUS)
    if summary:
        s["amazon_order_forecast_run_summary"] = set(SUMMARY) | ({"sanity_status", "sanity_warning"} if sanity_flags else set())
    return s


def norm(sql: str) -> str:
    return " ".join(sql.split())


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.fast_executemany, self._result = conn, False, []

    def execute(self, sql, params=()):
        c, s = self.conn, norm(sql)
        c.log.append(("execute", s, tuple(params)))
        if "INFORMATION_SCHEMA.COLUMNS" in s:
            if c.probe_fails:
                raise RuntimeError("permission denied on INFORMATION_SCHEMA")
            self._result = [(t, col) for t, cols in c.schema.items() for col in sorted(cols)]
        elif s.startswith("SELECT COL_LENGTH"):
            table, col = re.search(r"'dbo\.(\w+)', '(\w+)'", s).groups()
            self._result = [(4 if col in c.schema.get(table, set()) else None,)]
        elif s.startswith("INSERT INTO dbo.amazon_order_forecast_run "):
            c.next_id += 1
            mk, method, target, train_end = params[:4]
            c.runs.append(dict(id=c.next_id, marketplace=mk, method=method, target=target, train_end=train_end))
            self._result = [(c.next_id,)]
        elif s.startswith("DELETE"):
            mk, method, target, train_end = params
            hit = [r["id"] for r in c.runs if (r["marketplace"], r["method"], r["target"], r["train_end"])
                   == (mk, method, target, train_end)]
            table = re.search(r"FROM dbo\.(\w+)", s).group(1)
            c.deleted.append((table, tuple(hit)))
            if table == "amazon_order_forecast_run":
                c.runs = [r for r in c.runs if r["id"] not in hit]
        return self

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)

    def executemany(self, sql, rows):
        if not rows:
            raise ValueError("second parameter to executemany must not be empty")   # what pyodbc does
        if self.conn.fail_on_many:
            raise RuntimeError("boom")
        self.conn.log.append(("many", norm(sql), list(rows)))


class FakeConn:
    def __init__(self, schema_=None, runs=None, fail_on_many=False, probe_fails=False):
        self.schema = schema_ if schema_ is not None else schema()
        self.runs = [dict(r) for r in (runs or [])]
        self.next_id = max([r["id"] for r in self.runs], default=100)
        self.log, self.deleted, self.autocommit = [], [], True
        self.fail_on_many, self.probe_fails = fail_on_many, probe_fails
        self.committed = self.rolled_back = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True
        self.log.append(("commit",))

    def rollback(self):
        self.rolled_back = True
        self.log.append(("rollback",))

    def close(self):
        pass

    # helpers for assertions
    def many(self, table: str) -> list:
        return [x for x in self.log if x[0] == "many" and f"INSERT INTO dbo.{table} " in x[1]]

    def statements(self, prefix: str) -> list:
        return [x for x in self.log if x[0] == "execute" and x[1].startswith(prefix)]
