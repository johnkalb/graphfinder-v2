#!/usr/bin/env python3
"""One-shot loader: graph_scored.json.gz (+ shards) -> Postgres graph_nodes/graph_edges.

Populates the tables consumed by pathfinder._find_path_pg() (see
webapp/migrations/001_graph_pgrouting.sql for the schema). Mirrors
pathfinder._load_graph()'s edge-weight formula exactly (-log(prob) +
forwarding penalty) so pgr_ksp ranks paths the same way
nx.shortest_simple_paths does over the in-memory graph.

Usage:
    DATABASE_URL=postgresql://... python webapp/load_graph_to_postgres.py

Idempotent: truncates graph_nodes/graph_edges before loading, so it's safe
to rerun after a data refresh.
"""
from __future__ import annotations
import gzip
import io
import json
import math
import os
import sys
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"


def _forward_penalty():
    try:
        from link_scoring import FORWARD_PROB
    except ImportError:
        from webapp.link_scoring import FORWARD_PROB
    return -math.log(FORWARD_PROB) if FORWARD_PROB > 0 else 0.0


def _iter_shards():
    """Yields (nodes, edges) tuples, one per shard -- mirrors
    pathfinder._load_graph()'s shard/single-file/legacy fallback order."""
    shard_paths = sorted(DATA_DIR.glob("graph_scored_[0-9][0-9][0-9].json.gz"))
    if shard_paths:
        for shard_path in shard_paths:
            with gzip.open(shard_path, "rt", encoding="utf-8") as f:
                ed = json.load(f)
            yield ed["nodes"], ed["edges"]
        return
    spath = DATA_DIR / "graph_scored.json.gz"
    if spath.exists():
        with gzip.open(spath, "rt", encoding="utf-8") as f:
            ed = json.load(f)
        yield ed["nodes"], ed["edges"]
        return
    raise FileNotFoundError(f"no graph_scored*.json.gz found in {DATA_DIR}")


def _load_deceased_set():
    dpath = DATA_DIR / "deceased.json"
    if not dpath.exists():
        return set()
    with open(dpath, "r", encoding="utf-8") as f:
        return {k.lower() for k in json.load(f).keys()}


def main():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    import psycopg2

    fwd_penalty = _forward_penalty()
    deceased = _load_deceased_set()

    conn = psycopg2.connect(database_url)
    try:
        cur = conn.cursor()
        cur.execute("TRUNCATE graph_edges, graph_nodes RESTART IDENTITY CASCADE")

        node_buf = io.StringIO()
        edge_buf = io.StringIO()
        total_nodes = 0
        total_edges = 0
        edge_id = 0
        # Node ids are per-shard-local indices in the source JSON but must be
        # globally unique across shards once loaded (graph_edges.source/target
        # reference graph_nodes.id) -- offset each shard's local indices by
        # the running node count, same effect as nx.Graph() unioning shards
        # by node name in pathfinder._load_graph(), just via an explicit
        # offset instead of name-keyed dict merge.
        node_offset = 0
        for nodes, edges in _iter_shards():
            for local_idx, name in enumerate(nodes):
                gid = node_offset + local_idx
                is_dead = name.lower() in deceased
                node_buf.write(f"{gid}\t{_copy_escape(name)}\t{_copy_escape(name.lower())}\t{is_dead}\n")
                total_nodes += 1
            for u, v, prob, cats in edges:
                p = prob if prob > 1e-9 else 1e-9
                cost = -math.log(p) + fwd_penalty
                cats_literal = "{" + ",".join(_copy_escape_array_elem(c) for c in (cats or [])) + "}"
                edge_buf.write(
                    f"{edge_id}\t{node_offset + u}\t{node_offset + v}\t{cost!r}\t{cost!r}\t{prob!r}\t{cats_literal}\n"
                )
                edge_id += 1
                total_edges += 1
            node_offset += len(nodes)

            if node_buf.tell() > 32 * 1024 * 1024:
                node_buf.seek(0)
                cur.copy_from(node_buf, "graph_nodes", columns=("id", "name", "name_lower", "is_deceased"))
                node_buf = io.StringIO()
            if edge_buf.tell() > 32 * 1024 * 1024:
                edge_buf.seek(0)
                cur.copy_from(edge_buf, "graph_edges",
                               columns=("id", "source", "target", "cost", "reverse_cost", "prob", "cats"))
                edge_buf = io.StringIO()

        if node_buf.tell():
            node_buf.seek(0)
            cur.copy_from(node_buf, "graph_nodes", columns=("id", "name", "name_lower", "is_deceased"))
        if edge_buf.tell():
            edge_buf.seek(0)
            cur.copy_from(edge_buf, "graph_edges",
                           columns=("id", "source", "target", "cost", "reverse_cost", "prob", "cats"))

        conn.commit()
        print(f"loaded {total_nodes} nodes, {total_edges} edges")
    finally:
        conn.close()


def _copy_escape(s: str) -> str:
    """Escape a value for COPY's default text format (tab-separated)."""
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _copy_escape_array_elem(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


if __name__ == "__main__":
    main()
