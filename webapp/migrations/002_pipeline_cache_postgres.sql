-- Harvest-side cache tables, migrated off pipeline_cache.db (SQLite) after
-- two corruption incidents in 24h (2026-09-11/12) -- see
-- [[project_db_corruption_2026_09_11]] and the Postgres migration plan.
-- Consumed by src/data/pg_shim.py + src/data/db_manager.py once DATABASE_URL
-- is set in a harvester process's environment.
--
-- Deliberately a SEPARATE database from the app-side tables (webapp/db.py)
-- and the graph-serving tables (001_graph_pgrouting.sql) -- independent
-- blast radius, not a shared schema.
--
-- Run once against the target database:
--   psql "$DATABASE_URL" -f webapp/migrations/002_pipeline_cache_postgres.sql

CREATE TABLE IF NOT EXISTS sec_cache (
    cik       TEXT PRIMARY KEY,
    data      TEXT,
    timestamp TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS form_d_cache (
    url         TEXT PRIMARY KEY,
    xml_content TEXT,
    timestamp   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS adv_cache (
    key       TEXT PRIMARY KEY,
    data      TEXT,
    timestamp TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS form4_cache (
    url         TEXT PRIMARY KEY,
    xml_content TEXT,
    timestamp   TIMESTAMPTZ DEFAULT now()
);

-- The one table that gets a real surrogate key it didn't have in SQLite.
-- SQLite's `relationships` had no explicit integer PK -- several scripts
-- (harvest_cursors consumers, person_reconciliation.py, littlesis_person.py)
-- relied on SQLite's implicit `rowid` as a monotonic cursor/ordering key.
-- Postgres has no safe equivalent (`ctid` is explicitly not for this), so
-- `id BIGSERIAL` is the real fix, not a cosmetic rename -- calling code that
-- used `rowid` must be updated to reference `id` explicitly.
CREATE TABLE IF NOT EXISTS relationships (
    id            BIGSERIAL PRIMARY KEY,
    source_id     TEXT,
    source_name   TEXT,
    source_type   TEXT,  -- 'PERSON' or 'COMPANY'
    target_id     TEXT,
    target_name   TEXT,
    target_type   TEXT,
    relation_type TEXT,  -- 'DIRECTOR', 'OFFICER', '10% OWNER', etc. -- NOT a
                          -- clean enum in practice (free-text artifacts like
                          -- "DONATION ($250)", "POSITION (DIRECTOR)" exist in
                          -- real data) -- deliberately left as plain TEXT,
                          -- no CHECK constraint, so real rows aren't rejected.
    source_data   TEXT,  -- 'SEC', 'WIKIPEDIA', 'IRS_990', etc.
    evidence      TEXT   -- JSON: source URL/date/themes/etc., relation-type-dependent
);

-- NOT a plain UNIQUE(source_name, target_name, relation_type) -- a handful of
-- "names" are actually mis-parsed table dumps from PDF ingestion (pension-
-- holdings reports several KB long), which overflow btree's ~2.7KB row-size
-- limit if indexed directly (confirmed hitting this during the initial bulk
-- load: "index row size 3048 exceeds btree version 4 maximum 2704"). Same
-- fix already used for graph_nodes in 001_graph_pgrouting.sql: index the
-- md5 hashes instead -- fixed-width, can't overflow. Callers upserting
-- against this table target this index explicitly:
--   ON CONFLICT (md5(source_name), md5(target_name), relation_type) DO NOTHING
-- (src/data/db_manager.py's Postgres branch does this; pg_shim.py's generic
-- "INSERT OR IGNORE" rewrite emits a target-less ON CONFLICT DO NOTHING,
-- which matches this index -- or any other constraint violation -- with no
-- change needed there).
CREATE UNIQUE INDEX IF NOT EXISTS idx_rel_unique_md5
    ON relationships (md5(source_name), md5(target_name), relation_type);

-- The SQLite originals (idx_rel_person, idx_relationships_source_type_name,
-- idx_relationships_target_type_name) indexed the raw (type, name) pair --
-- same oversized-value overflow as the unique index above (confirmed hitting
-- this on idx_relationships_target_type_name during the bulk load, same
-- offending row). Checked every caller that queries relationships (the
-- nightly build_scored_edges.py, add_evidence.py, build_group_rankings.py,
-- check_org_board_coverage.py): none do an exact-name lookup filtered by
-- type together -- build_scored_edges.py's WHERE source_type='PERSON' /
-- target_type='PERSON' scans are the only consumers of these indexes, and
-- they only ever filter on the type column, never combine it with an exact
-- name match. So the name column adds no real value here and is dropped
-- instead of hashed -- index type alone.
CREATE INDEX IF NOT EXISTS idx_relationships_source_type ON relationships (source_type);
CREATE INDEX IF NOT EXISTS idx_relationships_target_type ON relationships (target_type);
CREATE INDEX IF NOT EXISTS idx_relationships_source_data ON relationships (source_data);
CREATE INDEX IF NOT EXISTS idx_relationships_relation_type ON relationships (relation_type);

CREATE TABLE IF NOT EXISTS relationships_quarantine (
    id                BIGSERIAL PRIMARY KEY,
    source_id         TEXT,
    source_name       TEXT,
    source_type       TEXT,
    target_id         TEXT,
    target_name       TEXT,
    target_type       TEXT,
    relation_type     TEXT,
    source_data       TEXT,
    evidence          TEXT,
    quarantine_reason TEXT,
    quarantined_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS harvest_cursors (
    source        TEXT PRIMARY KEY,
    cursor_key    TEXT,
    cursor_value  BIGINT,
    status        TEXT DEFAULT 'running',
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reconciliation_name_index (
    person_name TEXT,
    source      TEXT,
    source_id   TEXT,
    role        TEXT,
    details     TEXT
);

CREATE TABLE IF NOT EXISTS sec_form4_insider_trades (
    ticker TEXT
);

-- test_table intentionally not migrated (unused).
