"""Rebuild graph_name_cache: every distinct PERSON/ORG name in relationships,
kept apart by side (source vs target), so harvesters can ask "is this name
already in the graph?" without scanning relationships themselves.

Before this (2026-10-13), seven harvesters (irs_990, patent_inventor,
github_orgs, fec_contributions, littlesis, littlesis_person,
congress_legislators) ran SELECT DISTINCT source_name / target_name over the
287M-row relationships table on every tick -- 8-16 minutes per scan,
irs_990 every 15 minutes -- which kept the optiplex disk saturated. They now
read this table instead; a query like
    SELECT DISTINCT source_name FROM relationships WHERE source_type='PERSON'
becomes
    SELECT name FROM graph_name_cache WHERE role='source' AND entity_type='PERSON'
with the same rows, as of the last refresh. Refreshed once a day by
rebuild_and_deploy.py (and runnable by hand).

The new copy is built under another name and swapped in, so readers never
see a missing or half-built table.
"""
import os
import sys
import time

import psycopg2

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
import pg_secret  # noqa: E402

BUILD = """
DROP TABLE IF EXISTS graph_name_cache_new;
CREATE TABLE graph_name_cache_new AS  -- logged: an UNLOGGED table comes back empty after a crash, and harvesters would then treat every name as new
    SELECT DISTINCT 'source'::text AS role, source_type AS entity_type, source_name AS name
    FROM relationships WHERE source_type IN ('PERSON', 'ORG') AND source_name IS NOT NULL
    UNION ALL
    SELECT DISTINCT 'target'::text, target_type, target_name
    FROM relationships WHERE target_type IN ('PERSON', 'ORG') AND target_name IS NOT NULL;
CREATE INDEX ON graph_name_cache_new (role, entity_type);
ANALYZE graph_name_cache_new;
"""
SWAP = """
DROP TABLE IF EXISTS graph_name_cache;
ALTER TABLE graph_name_cache_new RENAME TO graph_name_cache;
ALTER INDEX IF EXISTS graph_name_cache_new_role_entity_type_idx RENAME TO graph_name_cache_role_entity_type_idx;
"""


def main():
    t0 = time.time()
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute(BUILD)
    conn.commit()
    cur.execute(SWAP)
    conn.commit()
    cur.execute("SELECT role, entity_type, count(*) FROM graph_name_cache GROUP BY 1, 2 ORDER BY 1, 2")
    counts = ", ".join(f"{r}/{t}={n:,}" for r, t, n in cur.fetchall())
    print(f"graph_name_cache refreshed in {time.time() - t0:.0f}s: {counts}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
