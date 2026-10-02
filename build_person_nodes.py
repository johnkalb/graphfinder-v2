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
PEOPLE_TSV = "webapp/data/graph_people.tsv.gz"

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
    # Majority vote per name: a single stray PERSON typing isn't enough --
    # some source typed "Los Angeles" as PERSON, and it reached the GDELT
    # classifier as a "person" (2026-09-29). Count PERSON vs ORG typings of
    # each name across non-GDELT rows and call it a person only if PERSON wins
    # or ties.
    # RECONCILIATION is excluded too: its CROSS_REFERENCED self-loops re-type
    # GDELT NER names as PERSON (that's where "Los Angeles" came from), and
    # LOCATION typings count against being a person.
    votes = {}   # canon key -> [person_count, non_person_count]
    for col, typ in (("source_name", "source_type"), ("target_name", "target_type")):
        cur.execute(f"SELECT {col}, {typ}, count(*) FROM relationships "
                    f"WHERE {typ} IN ('PERSON', 'ORG', 'LOCATION') "
                    f"AND source_data NOT IN ('GDELT', 'GDELT_FULL', 'RECONCILIATION') GROUP BY 1, 2")
        for name, t, n in cur.fetchall():
            if name:
                v = votes.setdefault(canon_key(name), [0, 0])
                v[0 if t == "PERSON" else 1] += n
        print(f"  {col}: {len(votes):,} typed names so far ({time.time() - t0:.0f}s)", flush=True)
    conn.close()
    person_keys = {k for k, (p, o) in votes.items() if p and p >= o}
    print(f"  {len(person_keys):,} names typed PERSON at least as often as ORG", flush=True)

    with gzip.open(SCORED, "rt", encoding="utf-8") as f:
        g = json.load(f)
    nodes = g["nodes"]
    # Common-name split nodes ("MICHAEL SMITH (Fedex)", see
    # webapp/disambiguation.py) aren't in the DB under that display name, so
    # they take the typing of the name they were split from. Their employer
    # part would trip the org-word check, so it's applied to the base only.
    splits = {int(i): base for i, base in (g.get("splits") or {}).items()}
    # Some sources type companies as PERSON ("Jeepers Inc" reached #1,000 on the
    # PageRank ladder, 2026-09-29), so an obvious org word overrides the DB type.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_crawlie_facts import _ORG_WORDS
    idx = [i for i, n in enumerate(nodes)
           if (canon_key(splits[i]) in person_keys and not _ORG_WORDS.search(splits[i])) if i in splits
           else (canon_key(n) in person_keys and not _ORG_WORDS.search(n))]
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(idx, f, separators=(",", ":"))
    # canon_key -> display name, for the GDELT classifier on optiplex (copied
    # there by rebuild_and_deploy.py): lets it test "both names are graph
    # people" without a full-table scan, and write promoted relations under
    # the node's own display name.
    with gzip.open(PEOPLE_TSV, "wt", encoding="utf-8") as f:
        for i in idx:
            if i in splits:     # GDELT names can't say which "Michael Smith" they mean
                continue
            f.write(f"{canon_key(nodes[i])}\t{nodes[i]}\n")
    print(f"Wrote {OUT}: {len(idx):,} of {len(nodes):,} nodes are people ({time.time() - t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
