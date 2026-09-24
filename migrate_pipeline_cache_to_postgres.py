#!/usr/bin/env python3
"""One-shot + incremental loader: pipeline_cache.db (SQLite) -> Postgres.

Structured like webapp/load_graph_to_postgres.py (buffered COPY via
io.StringIO, flushed every ~32MB) -- same bulk-load pattern, different
source/target tables. See the Postgres migration plan and
[[project_db_corruption_2026_09_11]] for why.

Usage:
    # Phase A: one-time full load into an empty Postgres database.
    DATABASE_URL=postgresql://...  \\
        python migrate_pipeline_cache_to_postgres.py --full --sqlite-db path/to/pipeline_cache.db

    # Phase C: final delta sync just before cutover (relationships +
    # harvest_cursors only -- the only tables that matter once harvesters
    # are paused for the actual flip).
    DATABASE_URL=postgresql://...  \\
        python migrate_pipeline_cache_to_postgres.py --delta --sqlite-db path/to/pipeline_cache.db \\
            --state-file migrate_state.json

--full TRUNCATEs the target tables first (safe to rerun from scratch), so
it must only be used against a database nothing is depending on yet.
--delta tracks a per-table SQLite-rowid high-water-mark in --state-file and
upserts only what's new -- safe to run repeatedly, including after harvesters
have already been cut over to Postgres and are no longer touching the SQLite
source at all (in which case it's a no-op).
"""
from __future__ import annotations
import argparse
import io
import json
import os
import sqlite3
import sys
import time

# All nine live tables in pipeline_cache.db (test_table excluded -- unused).
# columns: exact column list in the SQLite schema, in order.
# conflict: the Postgres ON CONFLICT target for --delta upserts, or None to
#   skip conflict handling entirely (tables with no unique constraint --
#   duplicates were already possible in SQLite too, behavior is unchanged).
TABLES = [
    {"name": "sec_cache", "columns": ["cik", "data", "timestamp"], "conflict": "(cik) DO UPDATE SET data = EXCLUDED.data, timestamp = EXCLUDED.timestamp"},
    {"name": "form_d_cache", "columns": ["url", "xml_content", "timestamp"], "conflict": "(url) DO UPDATE SET xml_content = EXCLUDED.xml_content, timestamp = EXCLUDED.timestamp"},
    {"name": "adv_cache", "columns": ["key", "data", "timestamp"], "conflict": "(key) DO UPDATE SET data = EXCLUDED.data, timestamp = EXCLUDED.timestamp"},
    {"name": "form4_cache", "columns": ["url", "xml_content", "timestamp"], "conflict": "(url) DO UPDATE SET xml_content = EXCLUDED.xml_content, timestamp = EXCLUDED.timestamp"},
    # Conflict target is the md5-hash unique index (idx_rel_unique_md5), not
    # the raw columns -- see webapp/migrations/002_pipeline_cache_postgres.sql
    # for why (a handful of names are oversized mis-parsed PDF dumps that
    # overflow a plain btree index).
    {"name": "relationships", "columns": ["source_id", "source_name", "source_type", "target_id", "target_name", "target_type", "relation_type", "source_data", "evidence"], "conflict": "(md5(source_name), md5(target_name), relation_type) DO NOTHING"},
    {"name": "relationships_quarantine", "columns": ["source_id", "source_name", "source_type", "target_id", "target_name", "target_type", "relation_type", "source_data", "evidence", "quarantine_reason", "quarantined_at"], "conflict": None},
    {"name": "harvest_cursors", "columns": ["source", "cursor_key", "cursor_value", "status", "updated_at"], "conflict": "(source) DO UPDATE SET cursor_key = EXCLUDED.cursor_key, cursor_value = EXCLUDED.cursor_value, status = EXCLUDED.status, updated_at = EXCLUDED.updated_at"},
    {"name": "reconciliation_name_index", "columns": ["person_name", "source", "source_id", "role", "details"], "conflict": None},
    {"name": "sec_form4_insider_trades", "columns": ["ticker"], "conflict": None},
]

FLUSH_BYTES = 32 * 1024 * 1024
# Kept modest (not 50k+) because a handful of tables (sec_cache, form4_cache,
# adv_cache, form_d_cache) hold large blob-ish TEXT columns (raw filing
# XML/JSON, tens to hundreds of KB per row) -- a big fetchmany() batch on
# those tables allocates that much memory just for the fetch itself, on top
# of whatever the per-row flush logic below is doing.
BATCH_FETCH = 2_000


def _copy_escape(v):
    if v is None:
        return "\\N"
    s = str(v)
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def load_state(path):
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    if not path:
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def full_load(sqlite_path, pg_conn, only_table=None):
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    cur = pg_conn.cursor()
    max_rowids = {}

    tables = [t for t in TABLES if only_table is None or t["name"] == only_table]
    for t in tables:
        name, cols = t["name"], t["columns"]
        print(f"[{name}] truncating target...", flush=True)
        cur.execute(f'TRUNCATE {name} RESTART IDENTITY CASCADE')

        print(f"[{name}] exporting from SQLite (ordered by rowid)...", flush=True)
        scur = sconn.cursor()
        scur.execute(f'SELECT rowid, {", ".join(cols)} FROM {name} ORDER BY rowid')

        buf = io.StringIO()
        total = 0
        last_rowid = 0
        t0 = time.time()
        # fetchmany's batch size (BATCH_FETCH) is tuned for narrow tables
        # like relationships -- for blob-heavy tables (sec_cache etc., rows
        # averaging tens/hundreds of KB of raw filing XML/JSON), a single
        # 50,000-row batch can be several GB before this loop ever checks
        # whether it's time to flush. Check the buffer size after EVERY row,
        # not once per outer fetchmany() batch, so a big-row table flushes
        # promptly regardless of how many rows fetchmany happened to return.
        while True:
            rows = scur.fetchmany(BATCH_FETCH)
            if not rows:
                break
            for row in rows:
                last_rowid = row[0]
                buf.write("\t".join(_copy_escape(v) for v in row[1:]) + "\n")
                total += 1
                if buf.tell() > FLUSH_BYTES:
                    buf.seek(0)
                    cur.copy_from(buf, name, columns=cols)
                    buf = io.StringIO()
                    print(f"[{name}] {total} rows so far ({time.time()-t0:.0f}s)", flush=True)
        if buf.tell():
            buf.seek(0)
            cur.copy_from(buf, name, columns=cols)
        pg_conn.commit()
        max_rowids[name] = last_rowid
        print(f"[{name}] done: {total} rows loaded, max source rowid {last_rowid}", flush=True)

    sconn.close()
    return max_rowids


def delta_sync(sqlite_path, pg_conn, state):
    sconn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    cur = pg_conn.cursor()
    new_state = dict(state)

    # Only relationships and harvest_cursors matter for a Phase C cutover --
    # the other tables are byproduct caches, safe to catch up later or not
    # at all. Delta-syncing them anyway costs nothing since --delta is cheap
    # to rerun, so do all tables with a conflict target (skip the three that
    # have none -- they'd just accumulate duplicates on every rerun).
    for t in TABLES:
        if t["conflict"] is None:
            continue
        name, cols, conflict = t["name"], t["columns"], t["conflict"]
        last_rowid = state.get(name, 0)
        scur = sconn.cursor()
        scur.execute(
            f'SELECT rowid, {", ".join(cols)} FROM {name} WHERE rowid > ? ORDER BY rowid',
            (last_rowid,),
        )
        placeholders = ", ".join(["%s"] * len(cols))
        insert_sql = f'INSERT INTO {name} ({", ".join(cols)}) VALUES ({placeholders}) ON CONFLICT {conflict}'

        rows_synced = 0
        max_rowid = last_rowid
        while True:
            rows = scur.fetchmany(BATCH_FETCH)
            if not rows:
                break
            batch = [row[1:] for row in rows]
            cur.executemany(insert_sql, batch)
            pg_conn.commit()
            rows_synced += len(rows)
            max_rowid = rows[-1][0]
        if rows_synced:
            print(f"[{name}] delta-synced {rows_synced} rows (rowid {last_rowid} -> {max_rowid})", flush=True)
        new_state[name] = max_rowid

    sconn.close()
    return new_state


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite-db", required=True, help="path to the source pipeline_cache.db")
    ap.add_argument("--state-file", default="migrate_state.json", help="delta-sync high-water-mark state (per table, by source SQLite rowid)")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="one-time full load; TRUNCATEs target tables first")
    mode.add_argument("--delta", action="store_true", help="incremental sync since the last recorded rowid per table")
    ap.add_argument("--only-table", default=None, help="--full only: (re)load just this one table, skip the rest (for retrying a single failed table)")
    args = ap.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        sys.exit("DATABASE_URL not set")

    import psycopg2
    pg_conn = psycopg2.connect(database_url)
    try:
        if args.full:
            max_rowids = full_load(args.sqlite_db, pg_conn, only_table=args.only_table)
            if args.only_table:
                # Merge into existing state rather than clobbering other
                # tables' recorded rowids with a single-table run's result.
                merged = load_state(args.state_file)
                merged.update(max_rowids)
                max_rowids = merged
            save_state(args.state_file, max_rowids)
            print(f"Full load complete. State written to {args.state_file} for future --delta runs.")
        else:
            state = load_state(args.state_file)
            new_state = delta_sync(args.sqlite_db, pg_conn, state)
            save_state(args.state_file, new_state)
            print(f"Delta sync complete. State updated in {args.state_file}.")
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
