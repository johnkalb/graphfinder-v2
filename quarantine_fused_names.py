#!/usr/bin/env python3
"""One-time cleanup pass: find GDELT/GDELT_FULL relationship rows whose
source_name or target_name looks like a fused name+verb artifact (see
fused_name_filter.py) and move them into relationships_quarantine instead
of deleting outright, so they can be spot-checked before permanent removal.

Safe to re-run: already-quarantined rowids are skipped.
"""
import sqlite3
import sys
import time

from fused_name_filter import is_fused_name

DB_PATH = r"C:\Users\johnk\graphfinder-clean\data\pipeline_cache.db"
SOURCES = ("GDELT_FULL", "GDELT")
BATCH_COMMIT = 2000


def safe_commit(db):
    for attempt in range(15):
        try:
            db.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                print(f"  [locked] retrying commit in 2s (attempt {attempt+1}/15)...")
                time.sleep(2.0)
            else:
                raise
    raise sqlite3.OperationalError("DB remained locked after 15 retries")


def main():
    db = sqlite3.connect(DB_PATH, timeout=60.0)
    read_cur = db.cursor()
    write_cur = db.cursor()

    placeholders = ",".join("?" for _ in SOURCES)
    read_cur.execute(
        f"SELECT rowid, source_name, target_name FROM relationships "
        f"WHERE source_data IN ({placeholders})",
        SOURCES,
    )

    scanned = 0
    flagged = 0
    pending_quarantine = []
    pending_delete_rowids = []
    t0 = time.time()

    for rowid, source_name, target_name in read_cur:
        scanned += 1
        reason = None
        if is_fused_name(source_name):
            reason = f"fused_source:{source_name}"
        elif is_fused_name(target_name):
            reason = f"fused_target:{target_name}"

        if reason:
            flagged += 1
            pending_quarantine.append((rowid, reason))
            pending_delete_rowids.append(rowid)

        if len(pending_delete_rowids) >= BATCH_COMMIT:
            flush(write_cur, db, pending_quarantine, pending_delete_rowids)
            pending_quarantine.clear()
            pending_delete_rowids.clear()

        if scanned % 500000 == 0:
            elapsed = time.time() - t0
            print(f"  scanned {scanned:,} rows, flagged {flagged:,} so far ({elapsed:.0f}s)")

    if pending_delete_rowids:
        flush(write_cur, db, pending_quarantine, pending_delete_rowids)

    elapsed = time.time() - t0
    print(f"\nDone. Scanned {scanned:,} rows from {SOURCES} in {elapsed:.0f}s.")
    print(f"Quarantined {flagged:,} rows.")
    db.close()


def flush(write_cur, db, pending_quarantine, pending_delete_rowids):
    # One INSERT per flagged row (targeted by rowid) -- pending_quarantine is
    # small relative to the scan size, so this is cheap; avoids a fragile
    # dynamic CASE expression for carrying the per-row reason string.
    for rowid, reason in pending_quarantine:
        write_cur.execute(
            """INSERT INTO relationships_quarantine
               (source_id, source_name, source_type, target_id, target_name,
                target_type, relation_type, source_data, evidence, quarantine_reason)
               SELECT source_id, source_name, source_type, target_id, target_name,
                      target_type, relation_type, source_data, evidence, ?
               FROM relationships WHERE rowid = ?""",
            (reason, rowid),
        )
    rowid_placeholders = ",".join("?" for _ in pending_delete_rowids)
    write_cur.execute(
        f"DELETE FROM relationships WHERE rowid IN ({rowid_placeholders})",
        pending_delete_rowids,
    )
    safe_commit(db)


if __name__ == "__main__":
    main()
