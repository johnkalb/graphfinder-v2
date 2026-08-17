-- pgRouting-backed graph store, replacing the in-memory NetworkX graph that
-- pathfinder._load_graph() rebuilds from graph_scored.json.gz on cold start.
-- See webapp/pathfinder.py:_find_path_pg() and webapp/load_graph_to_postgres.py.
--
-- Run once per database:
--   psql "$DATABASE_URL" -f webapp/migrations/001_graph_pgrouting.sql

CREATE EXTENSION IF NOT EXISTS pgrouting CASCADE;

CREATE TABLE IF NOT EXISTS graph_nodes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    name_lower  TEXT NOT NULL,
    is_deceased BOOLEAN NOT NULL DEFAULT FALSE
);

-- A handful of source "names" are actually mis-parsed table dumps from PDF
-- ingestion (pension-holdings reports several KB long), which overflow
-- btree's ~2.7KB row-size limit if indexed directly. Index md5(name_lower)
-- instead -- fixed-width, so it can't overflow -- and have callers match on
-- md5(name_lower) = md5($1) AND name_lower = $1 (see
-- pathfinder._find_path_pg's resolve()).
CREATE INDEX IF NOT EXISTS graph_nodes_name_lower_md5_idx ON graph_nodes (md5(name_lower));

-- cost/reverse_cost mirror pathfinder._load_graph()'s edge weight:
-- -log(prob) + forwarding penalty (-log(FORWARD_PROB)), computed once at
-- load time so pgr_ksp can consume it directly instead of recomputing per
-- query. Graph is undirected, so reverse_cost == cost.
CREATE TABLE IF NOT EXISTS graph_edges (
    id           BIGINT PRIMARY KEY,
    source       INTEGER NOT NULL REFERENCES graph_nodes (id),
    target       INTEGER NOT NULL REFERENCES graph_nodes (id),
    cost         DOUBLE PRECISION NOT NULL,
    reverse_cost DOUBLE PRECISION NOT NULL,
    prob         DOUBLE PRECISION NOT NULL,
    cats         TEXT[] NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS graph_edges_source_idx ON graph_edges (source);
CREATE INDEX IF NOT EXISTS graph_edges_target_idx ON graph_edges (target);
