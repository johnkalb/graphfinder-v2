#!/usr/bin/env python3
"""GDELT co-occurrence daily incremental classification -- Effort 1 of
specifications/gdelt-cooccurrence-classification.md.

Each run: finds people newly added to `relationships` by a real (non-GDELT)
source since the last run, looks up their GDELT_FULL co-occurrence partners
that are ALSO already-known real people, live-queries GDELT's DOC API for
fresh article URLs on each such pair, and runs them through the validated
corroboration pipeline (src/data/gdelt_classify.py). Promoted relations are
written back into `relationships` with source_data='GDELT_CORROBORATED'.

Postgres-only (uses relationships.id BIGSERIAL as the cursor watermark and
gdelt_cooccurrence_evidence's TEXT[] columns) -- requires DATABASE_URL set,
same as every other harvester post-migration (see
[[project_postgres_migration]]).

Deployed two places: this file (repo root, for local/manual runs) and a
mirrored copy at .hermes.old/agents/gdelt-full/scripts/ (for smart_runner's
SSH-remote dispatch to optiplex, agent key "gdelt" -- see smart_runner.py's
AGENTS dict). The sys.path setup below is OS-aware/absolute, same pattern as
gdelt_full_harvester.py, so it resolves src.* correctly from either
location -- os.path.dirname(__file__) would NOT (it'd point at .../scripts/
on the remote copy, which has no src/ alongside it)."""
import hashlib
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, "/home/john/.hermes" if os.name != "nt" else r"C:\Users\johnk\Desktop\sixdegrees")
from src.config import DB_PATH
from src.data import pg_shim
from src.data.pg_shim import connect as _pg_connect
from src.data.db_manager import DBManager
from src.data import gdelt_classify as gc

CURSOR_SOURCE = "gdelt_cooccurrence_daily"
CURSOR_KEY = "max_relationship_id"
GDELT_SOURCES = ("GDELT_FULL", "GDELT", "GDELT_CORROBORATED")
MAX_NEW_NAMES_PER_RUN = 500   # safety cap -- a normal day is tens to a couple hundred
MAX_PAIRS_PER_RUN = 60        # ~1-1.5 min/pair measured this session -> bounds a single run
# smart_runner kills the remote run at 1800s; stop starting new pairs well
# before that so the cursor gets saved. (60 pairs x 1-1.5 min never fit anyway.)
MAX_RUN_SECONDS = 20 * 60


def already_classified(db, pairs):
    """Pair hashes that already have a promoted/rejected verdict, so a name
    whose pairs span several runs doesn't get re-queried from the start."""
    if not pairs:
        return set()
    hashes = [pair_hash(a, b)[0] for a, b in pairs]
    cur = db.cursor()
    cur.execute(
        "SELECT pair_hash FROM gdelt_cooccurrence_evidence "
        "WHERE pair_hash = ANY(%s) AND promotion_status IN ('promoted', 'rejected')",
        (hashes,),
    )
    return {r[0] for r in cur.fetchall()}


def pair_hash(name_a, name_b):
    a, b = sorted((name_a.lower(), name_b.lower()))
    key = f"{a}|{b}"
    return hashlib.md5(key.encode("utf-8")).hexdigest(), a, b


def get_cursor(db):
    cur = db.cursor()
    cur.execute("SELECT cursor_value FROM harvest_cursors WHERE source = ?", (CURSOR_SOURCE,))
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    # First run: start from the current max id so we only classify people
    # added AFTER this script is first deployed, not the entire backlog.
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM relationships")
    start = int(cur.fetchone()[0])
    cur.execute(
        "INSERT OR IGNORE INTO harvest_cursors (source, cursor_key, cursor_value) VALUES (?, ?, ?)",
        (CURSOR_SOURCE, CURSOR_KEY, start),
    )
    db.commit()
    return start


def save_cursor(db, value):
    cur = db.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO harvest_cursors (source, cursor_key, cursor_value) VALUES (?, ?, ?)",
        (CURSOR_SOURCE, CURSOR_KEY, value),
    )
    db.commit()


def find_new_people(db, since_id):
    """People referenced by a non-GDELT relationship row with id > since_id.
    Returns (names_lowercased, max_id_seen). Known limitation: BIGSERIAL ids
    aren't a perfectly ordered commit watermark under concurrent writers, so
    a row committed late with an id below the new watermark could in theory
    be missed -- acceptable here since a missed pair simply waits for its
    next co-occurrence or Effort 3's later targeted sweep, not silently lost
    forever."""
    cur = db.cursor()
    cur.execute(
        """
        SELECT lower(name) AS name, MAX(id) AS max_id FROM (
            SELECT source_name AS name, id FROM relationships
            WHERE source_type = 'PERSON' AND id > %s
              AND source_data NOT IN ('GDELT_FULL', 'GDELT', 'GDELT_CORROBORATED')
            UNION ALL
            SELECT target_name AS name, id FROM relationships
            WHERE target_type = 'PERSON' AND id > %s
              AND source_data NOT IN ('GDELT_FULL', 'GDELT', 'GDELT_CORROBORATED')
        ) t
        WHERE name IS NOT NULL AND name != ''
        GROUP BY lower(name)
        ORDER BY max_id ASC
        LIMIT %s
        """,
        (since_id, since_id, MAX_NEW_NAMES_PER_RUN),
    )
    # [(name_lower, max_id)] in ascending max_id order -- the cursor may only
    # advance to a name's max_id once every pair for that name is handled.
    return [(r[0], int(r[1])) for r in cur.fetchall()]


def find_known_real_names(db):
    cur = db.cursor()
    cur.execute(
        "SELECT DISTINCT lower(source_name) FROM relationships "
        "WHERE source_type = 'PERSON' AND source_data NOT IN ('GDELT_FULL', 'GDELT', 'GDELT_CORROBORATED')"
    )
    known = set(r[0] for r in cur.fetchall() if r[0])
    cur.execute(
        "SELECT DISTINCT lower(target_name) FROM relationships "
        "WHERE target_type = 'PERSON' AND source_data NOT IN ('GDELT_FULL', 'GDELT', 'GDELT_CORROBORATED')"
    )
    known |= set(r[0] for r in cur.fetchall() if r[0])
    return known


def find_candidate_pairs(db, new_names, known_real_names):
    """For each newly-added person, find their GDELT_FULL co-occurrence
    partners that are ALSO already-known real people -- the highest-value
    slice for Effort 1: both endpoints already matter to the graph, we're
    only trying to find out WHAT specifically connects them."""
    if not new_names:
        return []
    cur = db.cursor()
    cur.execute(
        """
        SELECT
            CASE WHEN lower(source_name) = ANY(%(names)s) THEN source_name ELSE target_name END AS new_person,
            CASE WHEN lower(source_name) = ANY(%(names)s) THEN target_name ELSE source_name END AS partner
        FROM relationships
        WHERE source_data = 'GDELT_FULL'
          AND (lower(source_name) = ANY(%(names)s) OR lower(target_name) = ANY(%(names)s))
        """,
        {"names": new_names},
    )
    pairs = []
    seen = set()
    for row in cur.fetchall():
        new_person, partner = row[0], row[1]
        if not new_person or not partner:
            continue
        if partner.lower() not in known_real_names:
            continue
        if new_person.lower() == partner.lower():
            continue
        key = frozenset((new_person.lower(), partner.lower()))
        if key in seen:
            continue
        seen.add(key)
        pairs.append((new_person, partner))
    return pairs


def upsert_evidence_status(db, name_a, name_b, status, sample_urls):
    h, a, b = pair_hash(name_a, name_b)
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO gdelt_cooccurrence_evidence
            (pair_hash, name_a, name_b, occurrence_count, sample_urls, first_seen, last_seen, promotion_status)
        VALUES (%s, %s, %s, %s, %s, now(), now(), %s)
        ON CONFLICT (pair_hash) DO UPDATE SET
            promotion_status = EXCLUDED.promotion_status,
            last_seen = now()
        """,
        (h, name_a, name_b, len(sample_urls), sample_urls[:5], status),
    )
    db.commit()


def main():
    if not pg_shim.IS_POSTGRES:
        print("[gdelt-classify-daily] DATABASE_URL not set -- this job is Postgres-only. Aborting.")
        return

    db = _pg_connect(DB_PATH)
    dbm = DBManager(DB_PATH)

    since_id = get_cursor(db)
    print(f"[gdelt-classify-daily] Starting at {datetime.now().isoformat()}, cursor={since_id}")

    new_people = find_new_people(db, since_id)
    new_names = [n for n, _ in new_people]
    print(f"[gdelt-classify-daily] {len(new_names)} newly-added people since cursor")
    if not new_names:
        print("[gdelt-classify-daily] Nothing to do.")
        db.close()
        return

    known_real_names = find_known_real_names(db)
    print(f"[gdelt-classify-daily] {len(known_real_names)} known real (non-GDELT) names loaded")

    pairs = find_candidate_pairs(db, new_names, known_real_names)
    print(f"[gdelt-classify-daily] {len(pairs)} candidate pairs (new person <-> already-known partner)")

    # Group pending pairs by new person, skipping pairs that already have a
    # verdict from an earlier run. 2026-09-24: this used to classify only
    # pairs[:60] and then save the cursor past ALL ~500 names, so ~99% of
    # candidate pairs (and every pair that hit a GDELT 429) were never looked
    # at again -- 116K pairs seen Sep 14-24, ~1.4K attempted, 0 promoted.
    done = already_classified(db, pairs)
    pending_by_name = {}
    for name_a, name_b in pairs:
        if pair_hash(name_a, name_b)[0] not in done:
            pending_by_name.setdefault(name_a.lower(), []).append((name_a, name_b))
    print(f"[gdelt-classify-daily] {len(done)} already classified, "
          f"{sum(map(len, pending_by_name.values()))} pending")

    promoted_count = 0
    rejected_count = 0
    error_count = 0
    attempted = 0
    run_start = time.time()
    new_cursor = since_id
    stop_reason = None

    # Walk names in max_id order; the cursor only moves past a name once all
    # of its pairs have been handled, so anything cut off by the per-run
    # budget or a GDELT outage is picked up again next run.
    for name, name_max_id in new_people:
        for name_a, name_b in pending_by_name.get(name, []):
            if attempted >= MAX_PAIRS_PER_RUN or time.time() - run_start > MAX_RUN_SECONDS:
                stop_reason = "per-run budget reached"
                break
            attempted += 1
            print(f"[gdelt-classify-daily] [{attempted}] {name_a} <-> {name_b}...", flush=True)
            t0 = time.time()
            try:
                articles = gc.search_gdelt_comention(name_a, name_b)
                urls = [a["url"] for a in articles]
                if not urls:
                    print("    no candidate articles found")
                    upsert_evidence_status(db, name_a, name_b, "rejected", [])
                    rejected_count += 1
                    continue

                result = gc.classify_pair(name_a, name_b, urls)
                elapsed = time.time() - t0

                if result["promoted"]:
                    best_relation, supporting_urls = max(
                        result["promoted"].items(), key=lambda kv: len(kv[1])
                    )
                    relation_type = gc.normalize_relation_type(best_relation)
                    evidence = json.dumps({
                        "note": f"GDELT co-occurrence, corroborated by {len(supporting_urls)} independent sources",
                        "urls": supporting_urls,
                    })
                    dbm.add_relationship(
                        None, name_a, "PERSON", None, name_b, "PERSON",
                        relation_type, "GDELT_CORROBORATED", evidence,
                    )
                    upsert_evidence_status(db, name_a, name_b, "promoted", urls)
                    promoted_count += 1
                    print(f"    PROMOTED: {relation_type} ({len(supporting_urls)} sources, {elapsed:.1f}s)")
                else:
                    upsert_evidence_status(db, name_a, name_b, "rejected", urls)
                    rejected_count += 1
                    print(f"    not promoted ({result['n_independent']} independent source(s), {elapsed:.1f}s)")

            except gc.GdeltTransientError as e:
                # GDELT is down/throttling -- every remaining pair would fail
                # the same way, so stop and retry this one next run.
                error_count += 1
                stop_reason = f"GDELT unavailable ({e})"
                break
            except Exception as e:
                # Pair-specific failure (bad article, model error): skip it
                # rather than let one pair block the cursor forever.
                error_count += 1
                print(f"    ERROR: {e}")
        if stop_reason:
            break
        new_cursor = name_max_id

    save_cursor(db, new_cursor)
    print(
        f"\n[gdelt-classify-daily] Tick complete: {promoted_count} promoted, "
        f"{rejected_count} rejected, {error_count} errors. Cursor -> {new_cursor}"
        + (f" (stopped early: {stop_reason})" if stop_reason else "")
    )
    dbm.close()
    db.close()


if __name__ == "__main__":
    main()
