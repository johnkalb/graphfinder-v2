#!/usr/bin/env python3
"""GDELT co-occurrence classification, driven by stored evidence (2026-09-29).

Each run walks gdelt_cooccurrence_evidence -- the table gdelt_full_harvester.py
fills from GDELT's GKG files with every co-mentioned pair, a running mention
count, and up to 5 article URLs -- most-mentioned first, and for pairs where
BOTH names are people in the scored graph, runs the stored article URLs
through the validated corroboration pipeline (src/data/gdelt_classify.py).
Promoted relations are written to `relationships` with
source_data='GDELT_CORROBORATED', under the graph's own display names so they
attach to the existing nodes.

History: this used to live-query GDELT's DOC 2.0 search API for candidate
articles. That API has been overloaded since mid-September (GDELT's own
advice, 2026-09-21: use other datasets), so every query 429'd and the job was
paused 2026-09-24. The GKG files we already harvest carry the same article
URLs, so no search API is needed.

promotion_status values written here: promoted / rejected (articles read, no
corroborated relation) / no_articles (no stored URL could be fetched -- dead
links, mostly older articles) / not_graph_people (a name isn't a graph person;
recorded so the pair isn't rescanned every run; reset to 'unreviewed' to
revisit after the graph grows).

Graph people come from graph_people.tsv.gz (canon_key TAB display name),
written by build_person_nodes.py in the nightly rebuild and copied to optiplex.

Deployed two places: this file (repo root) and a mirrored copy at
.hermes.old/agents/gdelt-full/scripts/ that smart_runner scp's to optiplex
(agent key "gdelt"). Postgres-only."""
import gzip
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime

sys.path.insert(0, "/home/john/.hermes" if os.name != "nt" else r"C:\Users\johnk\Desktop\sixdegrees")
from src.config import DB_PATH
from src.data import pg_shim
from src.data.pg_shim import connect as _pg_connect
from src.data.db_manager import DBManager
from src.data import gdelt_classify as gc

GRAPH_PEOPLE = os.environ.get(
    "GRAPH_PEOPLE_PATH",
    "/home/john/.hermes/agents/gdelt-full/graph_people.tsv.gz" if os.name != "nt"
    else r"C:\Users\johnk\graphfinder-clean\webapp\data\graph_people.tsv.gz")
SCAN_BATCH = 2000
MAX_PAIRS_PER_RUN = 60
# smart_runner kills the remote run at 1800s; stop starting new pairs well before.
MAX_RUN_SECONDS = 20 * 60

_COMBINING = dict.fromkeys(range(0x300, 0x370))
_CK_KEEP = re.compile(r"[^\w ]|_")
_CK_WS = re.compile(r"\s+")


def canon_key(name):
    """Identical to build_scored_edges.canon_key (the graph's node-merge key)."""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = _CK_WS.sub(" ", _CK_KEEP.sub("", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


def load_graph_people():
    people = {}
    with gzip.open(GRAPH_PEOPLE, "rt", encoding="utf-8") as f:
        for line in f:
            key, _, display = line.rstrip("\n").partition("\t")
            if key:
                people[key] = display or key
    return people


def set_status(db, pair_hashes, status):
    if not pair_hashes:
        return
    cur = db.cursor()
    cur.execute("UPDATE gdelt_cooccurrence_evidence SET promotion_status = %s WHERE pair_hash = ANY(%s)",
                (status, list(pair_hashes)))
    db.commit()


def next_candidates(db, people, want):
    """Up to `want` unreviewed pairs with >=2 stored URLs whose names are both
    graph people, most-mentioned first. Pairs that fail the people check are
    marked not_graph_people as the scan passes them."""
    cur = db.cursor()
    out = []
    while len(out) < want:
        # Picked pairs stay 'unreviewed' until classified, so exclude them here
        # or a second batch would return them again.
        cur.execute(
            "SELECT pair_hash, name_a, name_b, occurrence_count, sample_urls FROM gdelt_cooccurrence_evidence "
            "WHERE promotion_status = 'unreviewed' AND cardinality(sample_urls) >= 2 "
            "AND NOT (pair_hash = ANY(%s)) ORDER BY occurrence_count DESC LIMIT %s",
            ([c[0] for c in out], SCAN_BATCH))
        rows = cur.fetchall()
        if not rows:
            break
        not_people = []
        for h, a, b, n, urls in rows:
            ka, kb = canon_key(a), canon_key(b)
            if ka in people and kb in people and ka != kb:
                out.append((h, people[ka], people[kb], n, list(urls)))
                if len(out) >= want:
                    break
            else:
                not_people.append(h)
        set_status(db, not_people, "not_graph_people")
        if len(out) < want and not not_people:
            break   # a whole batch of graph-people pairs -> they're all in `out`
    return out


def main():
    if not pg_shim.IS_POSTGRES:
        print("[gdelt-classify] DATABASE_URL not set -- this job is Postgres-only. Aborting.")
        return
    run_start = time.time()
    db = _pg_connect(DB_PATH)
    dbm = DBManager(DB_PATH)
    people = load_graph_people()
    print(f"[gdelt-classify] Starting at {datetime.now().isoformat()}; {len(people):,} graph people loaded", flush=True)

    candidates = next_candidates(db, people, MAX_PAIRS_PER_RUN)
    print(f"[gdelt-classify] {len(candidates)} candidate pairs this run", flush=True)
    counts = {"promoted": 0, "rejected": 0, "no_articles": 0, "errors": 0}
    for i, (h, name_a, name_b, n_mentions, urls) in enumerate(candidates, 1):
        if time.time() - run_start > MAX_RUN_SECONDS:
            print("[gdelt-classify] time budget reached; the rest stay unreviewed for the next run", flush=True)
            break
        print(f"[gdelt-classify] [{i}] {name_a} <-> {name_b} ({n_mentions:,} mentions, {len(urls)} urls)...", flush=True)
        t0 = time.time()
        try:
            result = gc.classify_pair(name_a, name_b, urls)
        except Exception as e:
            counts["errors"] += 1
            print(f"    ERROR: {e}")
            continue
        elapsed = time.time() - t0
        if not result.get("fetched"):
            set_status(db, [h], "no_articles")
            counts["no_articles"] += 1
            print(f"    no stored article could be fetched ({elapsed:.1f}s)")
        elif result["promoted"]:
            best_relation, supporting_urls = max(result["promoted"].items(), key=lambda kv: len(kv[1]))
            relation_type = gc.normalize_relation_type(best_relation)
            evidence = json.dumps({
                "note": f"GDELT co-occurrence, corroborated by {len(supporting_urls)} independent sources",
                "urls": supporting_urls, "mentions": n_mentions,
            })
            dbm.add_relationship(None, name_a, "PERSON", None, name_b, "PERSON",
                                 relation_type, "GDELT_CORROBORATED", evidence)
            set_status(db, [h], "promoted")
            counts["promoted"] += 1
            print(f"    PROMOTED: {relation_type} ({len(supporting_urls)} sources, {elapsed:.1f}s)")
        else:
            set_status(db, [h], "rejected")
            counts["rejected"] += 1
            print(f"    not promoted ({result['n_independent']} independent source(s) of "
                  f"{result.get('fetched', 0)} fetched, {elapsed:.1f}s)")

    print(f"\n[gdelt-classify] Tick complete: {counts['promoted']} promoted, {counts['rejected']} rejected, "
          f"{counts['no_articles']} no articles, {counts['errors']} errors ({time.time() - run_start:.0f}s)")
    dbm.close()
    db.close()


if __name__ == "__main__":
    main()
