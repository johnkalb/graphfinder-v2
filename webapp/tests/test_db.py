"""Tests for webapp/db.py: the dual-mode SQLite/Postgres connection layer.

Postgres-mode tests mock psycopg2 rather than requiring a live database --
this suite must stay runnable with no network/DB dependency, matching the
rest of the project's test suite. IS_POSTGRES is computed once at import
time from DATABASE_URL, so switching modes within a test requires reloading
the module with the env var patched first.
"""
import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import db as db_module  # noqa: E402


@pytest.fixture
def sqlite_mode(monkeypatch):
    """Reloads db.py with DATABASE_URL unset -- SQLite fallback mode."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reloaded = importlib.reload(db_module)
    yield reloaded
    importlib.reload(db_module)  # restore whatever mode the rest of the suite expects


@pytest.fixture
def postgres_mode(monkeypatch):
    """Reloads db.py with DATABASE_URL set -- Postgres mode. Resets the
    lazily-created pool afterward so other tests don't inherit it."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    reloaded = importlib.reload(db_module)
    yield reloaded
    monkeypatch.delenv("DATABASE_URL", raising=False)
    importlib.reload(db_module)


def test_is_postgres_false_when_database_url_unset(sqlite_mode):
    assert sqlite_mode.IS_POSTGRES is False


def test_is_postgres_true_when_database_url_set(postgres_mode):
    assert postgres_mode.IS_POSTGRES is True


def test_connect_falls_back_to_real_sqlite_when_no_database_url(sqlite_mode, tmp_path):
    conn = sqlite_mode.connect(str(tmp_path / "test.db"))
    try:
        import sqlite3
        assert isinstance(conn, sqlite3.Connection)
        cur = conn.execute("SELECT 1 AS one")
        row = cur.fetchone()
        assert row["one"] == 1  # sqlite3.Row dict-style access, matching prod code's usage
    finally:
        conn.close()


def test_pg_cursor_translates_question_marks_to_percent_s(postgres_mode):
    mock_real_cursor = MagicMock()
    cur = postgres_mode._PgCursor(mock_real_cursor)
    cur.execute("SELECT * FROM testers WHERE email = ? AND status = ?", ("a@example.com", "new"))
    mock_real_cursor.execute.assert_called_once_with(
        "SELECT * FROM testers WHERE email = %s AND status = %s",
        ("a@example.com", "new"),
    )


def test_pg_cursor_fetchone_fetchall_delegate_to_real_cursor(postgres_mode):
    mock_real_cursor = MagicMock()
    mock_real_cursor.fetchone.return_value = {"id": 1}
    mock_real_cursor.fetchall.return_value = [{"id": 1}, {"id": 2}]
    cur = postgres_mode._PgCursor(mock_real_cursor)
    assert cur.fetchone() == {"id": 1}
    assert cur.fetchall() == [{"id": 1}, {"id": 2}]


def test_pg_connection_close_returns_to_pool_not_real_close(postgres_mode):
    mock_pool = MagicMock()
    mock_real_conn = MagicMock()
    conn = postgres_mode._PgConnection(mock_pool, mock_real_conn)
    conn.close()
    mock_pool.putconn.assert_called_once_with(mock_real_conn)
    mock_real_conn.close.assert_not_called()


def test_pg_connection_row_factory_setter_is_a_harmless_noop(postgres_mode):
    conn = postgres_mode._PgConnection(MagicMock(), MagicMock())
    conn.row_factory = "anything"  # must not raise -- prod code does `conn.row_factory = sqlite3.Row`-shaped assignments in a few places
    assert conn.row_factory is None


def test_connect_uses_pool_in_postgres_mode(postgres_mode):
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = MagicMock()
    with patch.object(postgres_mode, "_get_pool", return_value=mock_pool):
        conn = postgres_mode.connect("ignored-path")
    assert isinstance(conn, postgres_mode._PgConnection)
    mock_pool.getconn.assert_called_once()


def test_get_pool_appends_sslmode_require_when_absent(postgres_mode, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    captured = {}

    class FakePool:
        def __init__(self, minconn, maxconn, dsn):
            captured["dsn"] = dsn

    with patch("psycopg2.pool.ThreadedConnectionPool", FakePool):
        postgres_mode._pool = None
        postgres_mode._get_pool()
    assert "sslmode=require" in captured["dsn"]
