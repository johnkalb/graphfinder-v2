"""Retire the old SEC_FORM_4 relationships that SEC_FORM_345 now covers (2026-10-20).

The old rows came from a harvest of 3 filings per company, with surname-first
names ('Russo Patricia F') and no dates; load_sec_insiders.py loaded every
director and officer from the SEC data sets with flipped names and dates. An
old row is covered when the same insider (flipped name, case-insensitive) has
a SEC_FORM_345 row at the same company (issuer CIK). Covered rows are copied
to relationships_form4_bak_20261020, then deleted, in one transaction; rows
not covered (entities, insiders missing from the data sets) stay.

  python retire_old_form4.py            # dry run: counts only
  python retire_old_form4.py --commit   # back up and delete

Undo: INSERT INTO relationships SELECT * FROM relationships_form4_bak_20261020;
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")   # pg_secret
import sec_insiders as si  # noqa: E402
from load_sec_insiders import connect  # noqa: E402

BACKUP = "relationships_form4_bak_20261020"


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = connect(readonly=not args.commit)
    cur = conn.cursor()
    cur.execute("SELECT evidence::json->>'sec_name', target_id FROM relationships WHERE source_data = 'SEC_FORM_345'")
    new = {(si.flip_name(n or "").lower(), (c or "").lstrip("0")) for n, c in cur.fetchall()}
    log(f"SEC_FORM_345 insider-company pairs: {len(new):,}")
    cur.execute("SELECT id, source_name, target_id FROM relationships WHERE source_data = 'SEC_FORM_4'")
    old = cur.fetchall()
    ids = [i for i, n, c in old if (si.flip_name(n or "").lower(), (c or "").lstrip("0")) in new]
    log(f"old SEC_FORM_4 rows: {len(old):,}; covered (to retire): {len(ids):,}; kept: {len(old) - len(ids):,}")
    if not args.commit:
        log("dry run -- nothing changed. Pass --commit to back up and delete.")
        return
    cur.execute("SELECT to_regclass(%s)", (BACKUP,))
    if cur.fetchone()[0]:
        sys.exit(f"{BACKUP} already exists -- not overwriting it")
    cur.execute(f"CREATE TABLE {BACKUP} AS SELECT * FROM relationships WHERE id = ANY(%s) AND source_data = 'SEC_FORM_4'",
                (ids,))
    cur.execute(f"SELECT count(*) FROM {BACKUP}")
    backed = cur.fetchone()[0]
    if backed != len(ids):
        conn.rollback()
        sys.exit(f"backup has {backed:,} rows, expected {len(ids):,} -- rolled back")
    cur.execute(f"DELETE FROM relationships WHERE id IN (SELECT id FROM {BACKUP}) AND source_data = 'SEC_FORM_4'")
    deleted = cur.rowcount
    if deleted != backed:
        conn.rollback()
        sys.exit(f"would delete {deleted:,} rows but backed up {backed:,} -- rolled back")
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = 'SEC_FORM_4'")
    log(f"backed up {backed:,} rows to {BACKUP}, deleted {deleted:,}; SEC_FORM_4 rows left: {cur.fetchone()[0]:,}")
    conn.close()


if __name__ == "__main__":
    main()
