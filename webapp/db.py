"""Dual-mode DB connection layer: sqlite3 locally (and for the whole test
suite, which never sets DATABASE_URL), pooled Postgres in production.

connect(db_path=None) is a drop-in replacement for sqlite3.connect(db_path):
in SQLite mode db_path is used exactly as before; in Postgres mode it's
ignored -- every caller's tables live in the one shared Postgres database
(table names don't collide across what used to be separate SQLite files).
"""
from __future__ import annotations
import os
import sqlite3

IS_POSTGRES = bool(os.environ.get("DATABASE_URL"))

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        import psycopg2.pool
        dsn = os.environ["DATABASE_URL"]
        if "sslmode=" not in dsn:
            dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, dsn)
    return _pool


class _PgCursor:
    def __init__(self, real_cursor):
        self._cur = real_cursor

    def execute(self, query, params=()):
        self._cur.execute(query.replace("?", "%s"), params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PgConnection:
    def __init__(self, pool, real_conn):
        self._pool = pool
        self._conn = real_conn

    def cursor(self):
        from psycopg2.extras import RealDictCursor
        return _PgCursor(self._conn.cursor(cursor_factory=RealDictCursor))

    def execute(self, query, params=()):
        # sqlite3.Connection.execute() convenience passthrough, used in a
        # few call sites instead of going through .cursor() first.
        return self.cursor().execute(query, params)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._pool.putconn(self._conn)

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # RealDictCursor already gives dict-like rows; nothing to set


def connect(db_path=None):
    if IS_POSTGRES:
        pool = _get_pool()
        return _PgConnection(pool, pool.getconn())
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn
