"""Which scored-graph nodes are people, from the database's own PERSON typing.

graph_scored.json.gz carries names only, and the looks_like_person() name
heuristic can't tell "DLA Piper" or "HCA Healthcare" from a person -- both
topped the first PageRank ladder (2026-09-28). The relationships table types
every endpoint, so a node is a person if some non-GDELT source typed that name
PERSON (GDELT's NER tags boilerplate like "whatsapp linkedin" as people).

Runs in rebuild_and_deploy.py right after build_scored_edges.py (cwd = the
build dir). Writes webapp/data/person_nodes.json.gz = sorted node indices;
build_search_from_scored.py (PageRank ladder) and build_graph_stats.py read it
and fall back to the name heuristic if it's missing. Build-dir only.
"""
import gzip
import json
import os
import re
import sys
import time
import unicodedata

import psycopg2

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/person_nodes.json.gz"

_COMBINING = dict.fromkeys(range(0x300, 0x370))
_CK_KEEP = re.compile(r"[^\w ]|_")
_CK_WS = re.compile(r"\s+")


def canon_key(name):
    """Identical to build_scored_edges.canon_key (the node-merge key)."""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = _CK_WS.sub(" ", _CK_KEEP.sub("", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


def database_url():
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import pg_secret
    return pg_secret.load_secret("DATABASE_URL")


def main():
    t0 = time.time()
    conn = psycopg2.connect(database_url())
    cur = conn.cursor()
    person_keys = set()
    for col, typ in (("source_name", "source_type"), ("target_name", "target_type")):
        cur.execute(f"SELECT DISTINCT {col} FROM relationships WHERE {typ} = 'PERSON' "
                    f"AND source_data NOT IN ('GDELT', 'GDELT_FULL')")
        person_keys |= {canon_key(r[0]) for r in cur.fetchall() if r[0]}
        print(f"  {col}: {len(person_keys):,} person keys so far ({time.time() - t0:.0f}s)", flush=True)
    conn.close()

    with gzip.open(SCORED, "rt", encoding="utf-8") as f:
        nodes = json.load(f)["nodes"]
    idx = [i for i, n in enumerate(nodes) if canon_key(n) in person_keys]
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(idx):,} of {len(nodes):,} nodes are people ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
