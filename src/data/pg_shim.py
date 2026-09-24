"""Dual-mode DB connection layer for the harvest-side pipeline_cache tables:
sqlite3 locally (the default -- every harvester keeps working unmodified
until DATABASE_URL is explicitly set), pooled Postgres once DATABASE_URL is
set in the calling process's environment.

Extracted 2026-09-12 from webapp/db.py's proven shim (already live in
production for mentioned_with_cache/llm_spend_tracker/service_items) so
src/data/db_manager.py and the handful of harvester scripts that bypass it
can share the same connect()/_PgCursor/_PgConnection logic instead of a
second hand-rolled copy. webapp/db.py is intentionally left as-is (its own
IS_POSTGRES/DATABASE_URL is a separate process's env var, pointed at a
different Postgres database -- the app-side one on DigitalOcean -- so the
two shims never collide even though they share a name).

One deliberate difference from webapp/db.py: this module does NOT force
sslmode=require onto the DSN. webapp/db.py targets DigitalOcean's managed
Postgres over the public internet, where TLS is the only thing between the
client and an untrusted network. The harvest-side Postgres instance (planned
host: optiplex, over Tailscale) sits behind a WireGuard tunnel that already
encrypts the transport, so the DSN's sslmode is left for the caller/operator
to decide explicitly (see migrations/002_pipeline_cache_postgres.sql and the
Postgres migration plan for the sslmode=disable rationale there).

connect(db_path=None) is a drop-in replacement for sqlite3.connect(db_path):
in SQLite mode db_path is used exactly as before; in Postgres mode it's
ignored -- every caller's tables live in the one shared Postgres database.

ALSO translates SQLite's non-standard upsert syntax generically, so ~50
independent harvester scripts (each opening sqlite3.connect() directly and
writing plain "INSERT OR IGNORE"/"INSERT OR REPLACE" SQL, discovered during
the Postgres migration to be the dominant pattern, not the DBManager-routed
minority originally assumed) become dual-mode just by swapping their
sqlite3.connect() call for pg_shim.connect() -- no per-file SQL rewriting:
  - "INSERT OR IGNORE INTO ..." -> "INSERT INTO ... ON CONFLICT DO NOTHING"
    (no conflict target needed -- matches SQLite's own "ignore whichever
    constraint fires" semantics exactly).
  - "INSERT OR REPLACE INTO t (c1, c2, ...) VALUES (...)" -> "... ON CONFLICT
    (c1) DO UPDATE SET c2 = EXCLUDED.c2, ...", assuming c1 (the first column
    in the explicit column list) is the table's primary key -- true for
    every OR REPLACE in this codebase (harvest_cursors.source, sec_cache.cik,
    org_missions.ein, etc. are always declared PRIMARY KEY and always listed
    first). If a future OR REPLACE breaks that convention this rewrite would
    target the wrong column -- worth a second look in review if one's added.
"""
from __future__ import annotations
import os
import re
import sqlite3

IS_POSTGRES = bool(os.environ.get("DATABASE_URL"))

_pool = None

_INSERT_OR_IGNORE_RE = re.compile(r"INSERT\s+OR\s+IGNORE\s+INTO", re.IGNORECASE)
_INSERT_OR_REPLACE_RE = re.compile(
    r"INSERT\s+OR\s+REPLACE\s+INTO\s+\w+\s*\(([^)]*)\)", re.IGNORECASE
)
# "SELECT name FROM sqlite_master WHERE type='table' AND name='X'" is the
# near-universal table-existence check across the ~50 converted harvester
# scripts (all templated from the same generator) -- sqlite_master is
# SQLite's own catalog table, no Postgres equivalent (that's
# information_schema.tables there). Confirmed identical wording (just the
# table-name literal varies) in every instance found across the codebase.
_SQLITE_MASTER_RE = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'([^']+)'",
    re.IGNORECASE,
)


def _rewrite_sqlite_upserts(query):
    m = _INSERT_OR_REPLACE_RE.match(query.strip())
    if m:
        cols = [c.strip() for c in m.group(1).split(",")]
        pk, rest_cols = cols[0], cols[1:]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in rest_cols)
        # Swap just the "INSERT OR REPLACE" tokens for "INSERT" -- the table
        # name / column list / VALUES(...) clause that follows is left
        # completely untouched, then append the upsert clause at the end.
        new_query = re.sub(r"INSERT\s+OR\s+REPLACE", "INSERT", query, count=1, flags=re.IGNORECASE)
        return new_query.rstrip().rstrip(";") + f" ON CONFLICT ({pk}) DO UPDATE SET {set_clause}"
    if _INSERT_OR_IGNORE_RE.match(query.strip()):
        new_query = re.sub(r"INSERT\s+OR\s+IGNORE", "INSERT", query, count=1, flags=re.IGNORECASE)
        return new_query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    m = _SQLITE_MASTER_RE.search(query)
    if m:
        table = m.group(1)
        return _SQLITE_MASTER_RE.sub(
            f"SELECT table_name AS name FROM information_schema.tables WHERE table_name='{table}'",
            query,
        )
    if re.match(r"\s*CREATE\s+TABLE", query, re.IGNORECASE):
        # DATETIME is a SQLite type-affinity name (SQLite's dynamic typing
        # accepts it loosely) with no Postgres equivalent -- confirmed
        # failing live ("type 'datetime' does not exist") the first time a
        # script created a genuinely NEW table (irs_990.py's org_missions --
        # most of the ~31 scripts using DATETIME in their own CREATE TABLE
        # IF NOT EXISTS never hit this because their table, e.g.
        # harvest_cursors, already exists, making the statement a no-op that
        # Postgres never actually type-checks). TIMESTAMP is the direct
        # portable equivalent. Scoped to CREATE TABLE statements only, not a
        # blanket replace, in case "DATETIME" ever appears in a data value.
        query = re.sub(r"\bDATETIME\b", "TIMESTAMP", query, flags=re.IGNORECASE)
    return query


def _get_pool():
    global _pool
    if _pool is None:
        import psycopg2.pool
        dsn = os.environ["DATABASE_URL"]
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 5, dsn)
    return _pool


class _Row:
    """Mimics sqlite3.Row: supports BOTH integer position (row[0]) and
    column-name (row["col"]) access, plus tuple-style unpacking/iteration
    (a, b, c = row). Needed because callers were written for two different
    row styles depending on when they were added -- the ~50 harvester
    scripts and build_scored_edges.py use positional access universally (as
    sqlite3.Row supports), while DBManager/webapp/db.py's original callers
    were written expecting RealDictCursor's dict-style access. A plain
    RealDictCursor row supports only the latter -- iterating or index-0'ing
    it yields dict KEYS, not values, which silently returns wrong data
    instead of raising. Wrapping every row this way makes both styles work
    under either backend, matching sqlite3.Row's actual behavior exactly."""
    # Keep the original mapping (already a dict) instead of eagerly copying
    # it into two fresh lists per row -- at multi-million-row scale (e.g.
    # person_reconciliation.py's unconditional full-table person-name scan,
    # confirmed ~8.2M rows/444s over Tailscale) that constant-factor
    # allocation adds up. Positional access still costs a values() list
    # build, but only when actually used, not for every row unconditionally.
    __slots__ = ("_mapping",)

    def __init__(self, mapping):
        self._mapping = mapping

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._mapping[key]
        return list(self._mapping.values())[key]

    def keys(self):
        return list(self._mapping.keys())

    def __iter__(self):
        return iter(self._mapping.values())

    def __len__(self):
        return len(self._mapping)

    def __repr__(self):
        return repr(dict(self._mapping))


def _translate_pg_error(real_conn, e):
    """Many of the ~50 converted harvester scripts catch specific
    sqlite3.* exception types (IntegrityError for duplicate-key swallowing,
    OperationalError for "database is locked" commit-retry loops) --
    Postgres raises its own psycopg2 exception hierarchy instead, which
    those existing except blocks don't match, AND (unlike SQLite) leaves the
    connection's transaction poisoned until rolled back. Roll back and
    re-raise as the nearest sqlite3 equivalent so every caller's existing
    exception handling works unchanged under either backend, with no
    per-script edits needed. Returns the exception to raise (never raises
    itself, so callers keep their own `raise ... from e` for a clean
    traceback)."""
    import psycopg2
    try:
        real_conn.rollback()
    except Exception:
        pass
    if isinstance(e, psycopg2.IntegrityError):
        return sqlite3.IntegrityError(str(e))
    if isinstance(e, psycopg2.OperationalError):
        return sqlite3.OperationalError(str(e))
    return sqlite3.Error(str(e))


class _PgCursor:
    def __init__(self, real_cursor):
        self._cur = real_cursor

    def execute(self, query, params=()):
        pg_query = _rewrite_sqlite_upserts(query).replace("?", "%s")
        import psycopg2
        try:
            self._cur.execute(pg_query, params)
        except psycopg2.Error as e:
            raise _translate_pg_error(self._cur.connection, e) from e
        return self

    def executemany(self, query, seq_of_params):
        # Without this override, executemany() falls through to __getattr__
        # and hits the raw psycopg2 cursor directly -- bypassing both the
        # ?->%s param-style rewrite and the INSERT OR IGNORE/REPLACE
        # rewrite entirely (confirmed: patent_inventor.py and
        # person_reconciliation.py both batch-insert via executemany()).
        pg_query = _rewrite_sqlite_upserts(query).replace("?", "%s")
        import psycopg2
        try:
            self._cur.executemany(pg_query, seq_of_params)
        except psycopg2.Error as e:
            raise _translate_pg_error(self._cur.connection, e) from e
        return self

    def fetchone(self):
        row = self._cur.fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(row) for row in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [_Row(row) for row in rows]

    def __iter__(self):
        return (_Row(row) for row in self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class _PgConnection:
    def __init__(self, pool, real_conn):
        self._pool = pool
        self._conn = real_conn

    def cursor(self):
        from psycopg2.extras import RealDictCursor
        return _PgCursor(self._conn.cursor(cursor_factory=RealDictCursor))

    def raw_cursor(self):
        """A genuine, unwrapped psycopg2 cursor on this SAME connection --
        for callers that need real psycopg2 cursor semantics
        get_raw_pg_connection() (a separate pooled connection) doesn't fit.
        Needed for psycopg2.extras.execute_values(): it builds its final
        query as raw bytes internally and calls cur.execute() with that,
        which _PgCursor.execute() can't accept (its regex-based sqlite-
        upsert rewrite expects a str) -- confirmed failing directly with
        "TypeError: cannot use a string pattern on a bytes-like object"
        when gdelt_full_harvester.py first tried batching its inserts this
        way (2026-09-13)."""
        return self._conn.cursor()

    def execute(self, query, params=()):
        # sqlite3.Connection.execute() convenience passthrough, used in a
        # few call sites instead of going through .cursor() first.
        return self.cursor().execute(query, params)

    def executemany(self, query, seq_of_params):
        # sqlite3.Connection.executemany() convenience passthrough --
        # adv_owners_harvest.py calls this directly on the connection
        # (same pattern as .execute() above), not via .cursor() first.
        return self.cursor().executemany(query, seq_of_params)

    def commit(self):
        import psycopg2
        try:
            self._conn.commit()
        except psycopg2.Error as e:
            raise _translate_pg_error(self._conn, e) from e

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
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def get_raw_pg_connection():
    """A plain (non-wrapped) psycopg2 connection from the same pool, for
    callers that need real psycopg2 cursor semantics or COPY support.
    Postgres-only; raises if DATABASE_URL isn't set."""
    if not IS_POSTGRES:
        raise RuntimeError("get_raw_pg_connection() requires DATABASE_URL (Postgres mode)")
    return _get_pool().getconn()


def release_raw_pg_connection(conn):
    _get_pool().putconn(conn)
