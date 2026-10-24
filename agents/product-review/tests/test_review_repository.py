"""ReviewRepository.fetch_analysed_uids(): which loaded reviews already have a
stored analysis. No database connection is made -- a fake connection holds
the rows and answers the two statements the method sends."""
import pytest

from daylight_sentiment.infra.db import (
    REVIEWS_TABLE,
    ExistingReviewsReadError,
    ReviewRepository,
)
from daylight_sentiment.infra.db import review_repository


class FakeConn:
    """Stored rows as {uid: sentiment}; table_exists=False mimics a brand-new
    database. Records every execute and whether the connection was closed."""

    def __init__(self, rows=None, table_exists=True, fail_on=None):
        self.rows = rows or {}
        self.table_exists = table_exists
        self.fail_on = fail_on
        self.executes = []
        self.closed = False

    def cursor(self):
        conn = self

        class Cursor:
            def execute(self, sql, params=()):
                conn.executes.append((sql, list(params)))
                if conn.fail_on and conn.fail_on in sql:
                    raise RuntimeError("Communication link failure")
                if sql.startswith("SELECT OBJECT_ID"):
                    self._result = [(1 if conn.table_exists else None,)]
                else:
                    self._result = [(u,) for u in params if conn.rows.get(u) is not None]

            def fetchone(self):
                return self._result[0]

            def fetchall(self):
                return self._result

        return Cursor()

    def close(self):
        self.closed = True


@pytest.fixture
def repo(monkeypatch):
    def make(conn):
        monkeypatch.setattr(review_repository, "connect", lambda settings: conn)
        return ReviewRepository(settings=None)
    return make


def test_sql_reads_only_rows_with_a_sentiment():
    sql = ReviewRepository.analysed_uids_sql(3)
    assert sql == (f"SELECT uid FROM dbo.{REVIEWS_TABLE} "
                   "WHERE sentiment IS NOT NULL AND uid IN (?, ?, ?)")


def test_returns_only_requested_uids_with_a_sentiment(repo):
    conn = FakeConn({"a": "positive", "b": None, "c": "negative", "z": "neutral"})
    found = repo(conn).fetch_analysed_uids(["a", "b", "c", "d"])
    # b failed analysis (sentiment NULL): not existing. z wasn't asked about.
    assert found == {"a", "c"}
    assert conn.closed


def test_no_uids_makes_no_connection(monkeypatch):
    def boom(settings):
        raise AssertionError("must not connect")
    monkeypatch.setattr(review_repository, "connect", boom)
    r = ReviewRepository(settings=None)
    assert r.fetch_analysed_uids([]) == set()
    assert r.fetch_analysed_uids(["", None]) == set()


def test_missing_table_means_nothing_is_stored(repo):
    conn = FakeConn(table_exists=False)
    assert repo(conn).fetch_analysed_uids(["a"]) == set()
    assert len(conn.executes) == 1 and conn.closed


def test_uids_are_queried_in_chunks_under_the_parameter_limit(repo):
    uids = [f"u{i:04d}" for i in range(2500)]
    conn = FakeConn({u: "positive" for u in uids[::2]})
    found = repo(conn).fetch_analysed_uids(uids + uids[:10], chunk_size=1000)
    assert found == set(uids[::2])
    lookups = [params for sql, params in conn.executes if "IN (" in sql]
    assert [len(p) for p in lookups] == [1000, 1000, 500]


@pytest.mark.parametrize("fail_on", ["OBJECT_ID", "IN ("])
def test_read_failure_raises_a_clear_error(repo, fail_on):
    conn = FakeConn({"a": "positive"}, fail_on=fail_on)
    with pytest.raises(ExistingReviewsReadError,
                       match=rf"dbo\.{REVIEWS_TABLE}: RuntimeError: Communication link"):
        repo(conn).fetch_analysed_uids(["a"])
    assert conn.closed


def test_connect_failure_raises_a_clear_error(monkeypatch):
    def refuse(settings):
        raise OSError("Login timeout expired")
    monkeypatch.setattr(review_repository, "connect", refuse)
    with pytest.raises(ExistingReviewsReadError, match="OSError: Login timeout expired"):
        ReviewRepository(settings=None).fetch_analysed_uids(["a"])
