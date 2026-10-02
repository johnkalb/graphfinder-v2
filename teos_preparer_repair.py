"""Re-attach IRS_990_TEOS officers to their nonprofit, not its tax preparer (2026-10-02).

irs_990.py took the FIRST BusinessNameLine1Txt in each return as the
organization, but the return header lists the paid preparer
(PreparerFirmGrp) before the Filer -- so officers of thousands of
foundations were linked to Ernst & Young, KPMG, Foundation Source and small
CPA firms, which became false hubs. The filer's EIN was read correctly and
is in every row's evidence, so each row's org is replaced by the IRS
Business Master File name for that EIN (ein_name.csv, EIN,NAME).

  python teos_preparer_repair.py            # dry run: counts + samples, rolls back
  python teos_preparer_repair.py --commit   # back up all IRS_990_TEOS rows, apply

Rules:
  * EIN in the BMF and the name differs beyond case/punctuation -> rename the
    target (and evidence.org) to the BMF name;
  * EIN not in the BMF and the stored name spans 2+ EINs (so it's a
    preparer, not the filer) -> delete, since the right org is unknown;
  * a rename that would duplicate an existing row (same person, org, type)
    deletes the row instead.
Undo: restore from relationships_teos_bak_20261002.
"""
import os
import sys
import time

import psycopg2

BACKUP = "relationships_teos_bak_20261002"
COMMIT = "--commit" in sys.argv


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")
if COMMIT:
    cur.execute("SELECT to_regclass(%s)", (BACKUP,))
    if cur.fetchone()[0] is None:
        cur.execute(f"CREATE TABLE {BACKUP} AS SELECT * FROM relationships WHERE source_data = 'IRS_990_TEOS'")
        conn.commit()
    cur.execute(f"SELECT count(*) FROM {BACKUP}")
    log(f"backup {BACKUP} holds {cur.fetchone()[0]:,} rows")

cur.execute("CREATE TEMP TABLE bmf (ein text PRIMARY KEY, name text)")
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ein_name.csv"), encoding="utf-8") as f:
    cur.copy_expert("COPY bmf FROM STDIN WITH (FORMAT csv)", f)
cur.execute("""
    CREATE TEMP TABLE teos AS
    SELECT r.id, r.source_name, r.target_name, r.relation_type,
           substring(r.evidence from '"ein":\\s*"([0-9]+)"') AS ein
    FROM relationships r WHERE r.source_data = 'IRS_990_TEOS'
""")
cur.execute("""
    CREATE TEMP TABLE plan AS
    SELECT t.id, t.source_name, t.target_name AS old_name, b.name AS new_name,
           CASE WHEN b.name IS NULL THEN 'delete' ELSE 'rename' END AS action
    FROM teos t
    LEFT JOIN bmf b ON b.ein = t.ein
    WHERE (b.name IS NOT NULL
           AND regexp_replace(upper(t.target_name), '[^A-Z0-9]', '', 'g')
               <> regexp_replace(upper(b.name), '[^A-Z0-9]', '', 'g'))
       OR (b.name IS NULL AND t.target_name IN (
              SELECT target_name FROM teos WHERE ein IS NOT NULL GROUP BY 1 HAVING count(DISTINCT ein) >= 2))
""")
cur.execute("CREATE INDEX ON plan (id)")
# a rename landing on a row that already exists (same person, org, type) -> delete instead
cur.execute("""
    UPDATE plan p SET action = 'delete_dup'
    FROM relationships r, relationships x
    WHERE p.action = 'rename' AND r.id = p.id
      AND md5(x.source_name) = md5(r.source_name) AND md5(x.target_name) = md5(p.new_name)
      AND x.relation_type = r.relation_type AND x.id <> r.id
""")
cur.execute("""
    UPDATE plan p SET action = 'delete_dup' FROM (
        SELECT p2.id, row_number() OVER (PARTITION BY md5(r.source_name), md5(p2.new_name), r.relation_type
                                         ORDER BY p2.id) AS rn
        FROM plan p2 JOIN relationships r ON r.id = p2.id WHERE p2.action = 'rename') d
    WHERE d.id = p.id AND d.rn > 1
""")
cur.execute("SELECT action, count(*) FROM plan GROUP BY 1 ORDER BY 1")
for a, n in cur.fetchall():
    log(f"  {a:<10} {n:>7,}")
cur.execute("SELECT count(*) FROM teos")
log(f"  of {cur.fetchone()[0]:,} IRS_990_TEOS rows")
cur.execute("SELECT source_name, old_name, new_name FROM plan WHERE action = 'rename' ORDER BY random() LIMIT 8")
for row in cur.fetchall():
    log(f"  e.g. {row[0]} | {row[1]} -> {row[2]}")

if not COMMIT:
    conn.rollback()
    log("dry run -- rolled back")
    sys.exit(0)

cur.execute("DELETE FROM relationships r USING plan p WHERE r.id = p.id AND p.action IN ('delete', 'delete_dup')")
log(f"deleted {cur.rowcount:,}")
cur.execute("""
    UPDATE relationships r
    SET target_name = p.new_name,
        evidence = CASE WHEN r.evidence IS NOT NULL AND r.evidence LIKE '{%%'
                        THEN jsonb_set(r.evidence::jsonb, '{org}', to_jsonb(p.new_name))::text
                        ELSE r.evidence END
    FROM plan p WHERE r.id = p.id AND p.action = 'rename'
""")
log(f"renamed {cur.rowcount:,}")
conn.commit()
log("committed")
