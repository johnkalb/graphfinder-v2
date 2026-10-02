"""Relabel LITTLESIS relationship rows with LittleSis's own category (2026-10-02).

littlesis_bulk_import.py mapped LittleSis category ids 2-12 to the wrong
relation types (e.g. 5 Donation -> BOARD: 1.25M donations scored as board
seats), and littlesis.py had 6-12 wrong. LittleSis's categories:
  1 Position 2 Education 3 Membership 4 Family 5 Donation 6 Transaction
  7 Lobbying 8 Social 9 Professional 10 Ownership 11 Hierarchy 12 Generic

Only rows whose stored type is one of the two importers' coarse labels are
touched; job-title types written from LittleSis descriptions (DIRECTOR, CEO)
are left alone. Each row's category comes from its evidence
relationship_id, looked up in rid_cat.csv (from the June 2026 bulk dump).

  python littlesis_relabel.py            # dry run: plan + counts, rolls back
  python littlesis_relabel.py --commit   # back up all LITTLESIS rows, apply

Rows whose corrected label duplicates an existing row (same pair, same
type) are deleted rather than relabelled, because the unique index forbids
the duplicate. Undo: restore from relationships_littlesis_bak_20261002.
"""
import os
import sys
import time

import psycopg2

TARGET = {1: "POSITION", 2: "EDUCATION", 3: "MEMBERSHIP", 4: "FAMILY", 5: "DONATION", 6: "TRANSACTION",
          7: "LOBBYING", 8: "SOCIAL", 9: "PROFESSIONAL", 10: "OWNERSHIP", 11: "HIERARCHY", 12: "ORGANIZATION"}
# every label either importer could have written from a category id
COARSE = ["POSITION", "OWNERSHIP", "FAMILY", "DONATION", "BOARD", "LOBBYING", "POLITICAL", "PROFESSIONAL",
          "SOCIAL", "EDUCATION", "MEMBERSHIP", "ORGANIZATION", "FUNDRAISING", "SUBSIDIARY", "OTHER"]
BACKUP = "relationships_littlesis_bak_20261002"
COMMIT = "--commit" in sys.argv


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")

if COMMIT:
    cur.execute("SELECT to_regclass(%s)", (BACKUP,))
    if cur.fetchone()[0] is None:
        log(f"backing up LITTLESIS rows to {BACKUP}...")
        cur.execute(f"CREATE TABLE {BACKUP} AS SELECT * FROM relationships WHERE source_data = 'LITTLESIS'")
        conn.commit()
    cur.execute(f"SELECT count(*) FROM {BACKUP}")
    log(f"backup holds {cur.fetchone()[0]:,} rows")

cur.execute("CREATE TEMP TABLE ls_cat (rid bigint PRIMARY KEY, cat int)")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "rid_cat.csv")) as f:
    cur.copy_expert("COPY ls_cat FROM STDIN WITH (FORMAT csv)", f)
cur.execute("CREATE TEMP TABLE ls_target (cat int PRIMARY KEY, new_type text)")
cur.executemany("INSERT INTO ls_target VALUES (%s, %s)", list(TARGET.items()))
log("planning...")
cur.execute("""
    CREATE TEMP TABLE plan AS
    SELECT r.id, r.relation_type AS old_type, t.new_type,
           md5(r.source_name) AS s, md5(r.target_name) AS tg, false AS del
    FROM relationships r
    JOIN ls_cat c ON c.rid = substring(r.evidence from '"relationship_id":\\s*([0-9]+)')::bigint
    JOIN ls_target t ON t.cat = c.cat
    WHERE r.source_data = 'LITTLESIS' AND r.relation_type = ANY(%s) AND r.relation_type <> t.new_type
""", (COARSE,))
cur.execute("CREATE INDEX ON plan (id)")
cur.execute("CREATE INDEX ON plan (s, tg, new_type)")
cur.execute("ANALYZE plan")
# two rows of the plan landing on the same (pair, type): keep the lowest id
cur.execute("""
    UPDATE plan p SET del = true FROM (
        SELECT id, row_number() OVER (PARTITION BY s, tg, new_type ORDER BY id) AS rn FROM plan) d
    WHERE d.id = p.id AND d.rn > 1
""")
log(f"  {cur.rowcount:,} duplicates within the plan")
# corrected label already present on a row the plan doesn't touch
cur.execute("""
    UPDATE plan p SET del = true
    WHERE NOT p.del AND EXISTS (
        SELECT 1 FROM relationships x
        WHERE md5(x.source_name) = p.s AND md5(x.target_name) = p.tg AND x.relation_type = p.new_type
          AND NOT EXISTS (SELECT 1 FROM plan q WHERE q.id = x.id))
""")
log(f"  {cur.rowcount:,} would duplicate an existing row")
cur.execute("SELECT old_type, new_type, count(*), sum(del::int) FROM plan GROUP BY 1, 2 ORDER BY 3 DESC")
for old, new, n, d in cur.fetchall():
    log(f"  {old:>13} -> {new:<13} {n:>10,}  ({d:,} deleted as duplicates)")
cur.execute("SELECT count(*), sum(del::int) FROM plan")
n, d = cur.fetchone()
log(f"plan: {n:,} rows ({n - d:,} relabelled, {d:,} deleted)")

if not COMMIT:
    conn.rollback()
    log("dry run -- rolled back")
    sys.exit(0)

# Relabel via a unique placeholder first, so swaps (BOARD->DONATION while a
# DONATION row for the same pair becomes FAMILY) never trip the unique index.
cur.execute("UPDATE relationships r SET relation_type = '__lsfix_' || r.id FROM plan p WHERE r.id = p.id")
log(f"phase 1: {cur.rowcount:,} rows parked")
cur.execute("DELETE FROM relationships r USING plan p WHERE r.id = p.id AND p.del")
log(f"phase 2: {cur.rowcount:,} duplicate rows deleted")
cur.execute("UPDATE relationships r SET relation_type = p.new_type FROM plan p WHERE r.id = p.id AND NOT p.del")
log(f"phase 3: {cur.rowcount:,} rows relabelled")
cur.execute("SELECT count(*) FROM relationships WHERE relation_type LIKE '\\_\\_lsfix\\_%'")
left = cur.fetchone()[0]
if left:
    conn.rollback()
    sys.exit(f"ABORT: {left} placeholder rows left -- rolled back")
conn.commit()
log("committed")
