"""Retire the old source_data='FEC' donation rows (2026-10-04).

The old fec-contributions harvester took OTHER_ID as the recipient and
skipped every donation without one, so its 760K rows were the earmarked
(ActBlue/WinRed) slice only. FEC_INDIV (fec_individual_harvest.py) replaces
them. Run this only after FEC_INDIV is loaded; it refuses otherwise.

  python fec_retire_old.py            # counts only
  python fec_retire_old.py --commit   # back up to relationships_fec_old_bak_20261004, then delete
Undo: INSERT INTO relationships SELECT * FROM relationships_fec_old_bak_20261004;
"""
import os
import sys

import psycopg2

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
BACKUP = "relationships_fec_old_bak_20261004"

url = os.environ.get("DATABASE_URL")
if not url:
    import pg_secret
    url = pg_secret.load_secret("DATABASE_URL")
conn = psycopg2.connect(url)
cur = conn.cursor()
cur.execute("SET statement_timeout = 0")
cur.execute("SELECT source_data, count(*) FROM relationships WHERE source_data IN ('FEC', 'FEC_INDIV') GROUP BY 1")
counts = dict(cur.fetchall())
print(f"old FEC rows: {counts.get('FEC', 0):,}; new FEC_INDIV rows: {counts.get('FEC_INDIV', 0):,}")
if counts.get("FEC_INDIV", 0) < 1_000_000:
    sys.exit("FEC_INDIV isn't loaded (fewer than 1M rows) -- load it first, nothing changed")
if "--commit" not in sys.argv:
    print("dry run -- pass --commit to back up and delete the old rows")
    sys.exit(0)
cur.execute("SELECT to_regclass(%s)", (BACKUP,))
if cur.fetchone()[0] is None:
    cur.execute(f"CREATE TABLE {BACKUP} AS SELECT * FROM relationships WHERE source_data = 'FEC'")
cur.execute(f"SELECT count(*) FROM {BACKUP}")
backed_up = cur.fetchone()[0]
if backed_up < counts.get("FEC", 0):
    conn.rollback()
    sys.exit(f"backup has {backed_up:,} rows, fewer than the {counts['FEC']:,} to delete -- nothing deleted")
cur.execute("DELETE FROM relationships WHERE source_data = 'FEC'")
deleted = cur.rowcount
conn.commit()
print(f"backed up {backed_up:,} rows to {BACKUP}; deleted {deleted:,}")
