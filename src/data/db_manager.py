import atexit
import sqlite3
import json
import os

from . import pg_shim

class DBManager:
    """NOTE (2026-09-12): every method here used to open a brand-new
    sqlite3.connect() and close it again on every single call. Under a
    tight loop (e.g. harvest_irs_bulk.py's per-row add_relationship calls --
    4,137 of them in ~92s, ~45 connects/sec) run concurrently with another
    harvester's own long-lived connection, that connect/disconnect churn is
    the prime suspect for a "database disk image is malformed" incident on
    pipeline_cache.db (WAL mode was already active at the time, so the
    connection churn itself -- not journal mode -- is the leading theory).
    Fix: one lazily-opened connection reused for the life of this object,
    with WAL mode and a busy_timeout set explicitly so a lock collision
    waits and retries instead of erroring. Call .close() when done with a
    long batch (also runs automatically at interpreter exit as a backstop).

    NOTE (2026-09-12, later same day): dual-mode via pg_shim.connect() --
    set DATABASE_URL in the process environment to route every call here at
    Postgres instead of the SQLite file at db_path (which is then ignored).
    This is the migration path off SQLite entirely: see the Postgres
    migration plan and [[project_db_corruption_2026_09_11]] for why -- two
    corruption incidents in 24h, root-caused to SQLite's single-writer-file
    model not holding up under ~15-30 independent, uncoordinated harvester
    processes, a workload Postgres's client-server model doesn't have this
    failure class for at all."""

    def __init__(self, db_path="data/pipeline_cache.db"):
        self.db_path = db_path
        # Ensure the directory for the database exists (SQLite mode only --
        # harmless no-op work in Postgres mode since db_path is unused there).
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._conn = None
        self._init_db()
        atexit.register(self.close)

    def _connect(self):
        if self._conn is None:
            self._conn = pg_shim.connect(self.db_path)
            if not pg_shim.IS_POSTGRES:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA busy_timeout=60000")
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _init_db(self):
        if pg_shim.IS_POSTGRES:
            # Schema is owned by migrations/002_pipeline_cache_postgres.sql,
            # applied once when the Postgres database is stood up -- not
            # recreated per DBManager instantiation the way SQLite's
            # CREATE TABLE IF NOT EXISTS convenience does below.
            return
        conn = self._connect()
        cursor = conn.cursor()
        # Create cache table for raw SEC payloads
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sec_cache (
                cik TEXT PRIMARY KEY,
                data TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Create parsed relationships table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT,
                source_name TEXT,
                source_type TEXT, -- 'PERSON' or 'COMPANY'
                target_id TEXT,
                target_name TEXT,
                target_type TEXT,
                relation_type TEXT, -- 'DIRECTOR', 'OFFICER', '10% OWNER', etc.
                source_data TEXT, -- 'SEC' or 'WIKIPEDIA'
                evidence TEXT, -- JSON: source URL/date/themes/etc., relation-type-dependent
                UNIQUE(source_name, target_name, relation_type)
            )
        """)
        conn.commit()

    def get_sec_cache(self, cik):
        conn = self._connect()
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM sec_cache WHERE cik = ?", (cik,))
        row = cursor.fetchone()
        if not row:
            return None
        data = row["data"] if pg_shim.IS_POSTGRES else row[0]
        return json.loads(data)

    def save_sec_cache(self, cik, data):
        conn = self._connect()
        cursor = conn.cursor()
        if pg_shim.IS_POSTGRES:
            cursor.execute(
                "INSERT INTO sec_cache (cik, data) VALUES (?, ?) "
                "ON CONFLICT (cik) DO UPDATE SET data = EXCLUDED.data",
                (cik, json.dumps(data)),
            )
        else:
            cursor.execute("INSERT OR REPLACE INTO sec_cache (cik, data) VALUES (?, ?)", (cik, json.dumps(data)))
        conn.commit()

    def add_relationship(self, src_id, src_name, src_type, tgt_id, tgt_name, tgt_type, relation, source_data, evidence=None):
        conn = self._connect()
        cursor = conn.cursor()
        params = (
            src_id,
            src_name.strip() if src_name else "",
            src_type,
            tgt_id,
            tgt_name.strip() if tgt_name else "",
            tgt_type,
            relation,
            source_data,
            evidence,
        )
        try:
            if pg_shim.IS_POSTGRES:
                # Targets idx_rel_unique_md5 (see
                # webapp/migrations/002_pipeline_cache_postgres.sql) -- a
                # handful of source/target "names" are actually mis-parsed
                # PDF table dumps too large for a plain btree index, so the
                # real unique index is on md5(source_name)/md5(target_name),
                # not the raw columns.
                cursor.execute("""
                    INSERT INTO relationships
                    (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (md5(source_name), md5(target_name), relation_type) DO NOTHING
                """, params)
            else:
                cursor.execute("""
                    INSERT OR IGNORE INTO relationships
                    (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, params)
            conn.commit()
        except Exception:
            # Postgres (unlike SQLite) poisons the rest of the transaction
            # after a failed statement -- since this connection is reused
            # for the object's lifetime, a bare `pass` here would silently
            # break every subsequent call on it. Roll back so the next
            # add_relationship() starts clean either way.
            try:
                conn.rollback()
            except Exception:
                pass
