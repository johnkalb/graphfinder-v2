"""Merge surname-first SEC insider rows into the SEC_FORM_345 people (2026-10-21).

Follow-up to retire_old_form4.py, after the hierarchical-community test found
insiders still split in two ('Hart Melanie M' and 'Melanie M Hart'):

- SEC_FORM_4 rows it kept: it matched only SEC_FORM_345 rows by issuer CIK
  and exact flipped spelling, so 'Ward Scott R.' (vs 'Ward Scott R' in the
  data sets) or a twin already loaded from another source (Zerhouni at
  Danaher, from Form D) stayed.
- RECONCILED_FORM4 rows ('INSIDER (INSIDER)'), written by
  person_reconciliation.py from SEC_FORM_4 names; no CIKs, the issuer CIK is
  in the filing URL.

A row is covered when the insider (flipped name, ignoring case, periods and
commas) has a SEC_FORM_345 row at the same company (issuer CIK, or the same
company name), or the flipped row already exists from any source. Covered
rows are deleted. A person row that isn't covered is renamed to the flipped
spelling -- SEC_FORM_345's own spelling when the insider is there under
another company -- so the two halves of a person become one node; entities
(funds, companies) are left alone. Every affected row is first copied to
relationships_secdup_bak_20261021; deletes and renames run in one transaction.

  python retire_sec_duplicates.py            # dry run: counts and samples
  python retire_sec_duplicates.py --commit   # back up, delete and rename

Undo: DELETE FROM relationships WHERE id IN (SELECT id FROM relationships_secdup_bak_20261021);
      INSERT INTO relationships SELECT * FROM relationships_secdup_bak_20261021;
"""
import argparse
import collections
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")   # pg_secret
import sec_insiders as si  # noqa: E402
from load_sec_insiders import connect  # noqa: E402

BACKUP = "relationships_secdup_bak_20261021"
SOURCES = ["SEC_FORM_4", "RECONCILED_FORM4"]
URL_CIK = re.compile(r"/edgar/data/(\d+)/")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def norm(name):
    return " ".join(re.sub(r"[.,]", " ", name or "").lower().split())


def key(sec_name):
    """Flipped and normalised: 'Ward Scott R.' and 'WARD SCOTT R' both give 'scott r ward'."""
    return norm(si.flip_name(sec_name or ""))


def cik_of(target_id, evidence):
    if target_id:
        return target_id.lstrip("0")
    try:
        m = URL_CIK.search(json.loads(evidence or "{}").get("source_url") or "")
    except ValueError:
        m = None
    return m.group(1).lstrip("0") if m else ""


def existing(cur, keys, batch=1000):
    """(source name, target name, relation type) triples already in the table, from any source."""
    found = set()
    keys = sorted(keys)
    for i in range(0, len(keys), batch):
        chunk = keys[i:i + batch]
        cur.execute("SELECT source_name, target_name, relation_type FROM relationships "
                    "WHERE (md5(source_name), md5(target_name), relation_type) IN "
                    "(SELECT md5(a), md5(b), c FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(a, b, c))",
                    ([k[0] for k in chunk], [k[1] for k in chunk], [k[2] for k in chunk]))
        found.update(cur.fetchall())
    return found


def plan(cur):
    cur.execute("SELECT source_name, evidence::json->>'sec_name', target_id, target_name "
                "FROM relationships WHERE source_data = 'SEC_FORM_345'")
    by_cik, by_org, spelling = set(), set(), {}
    for name, sec_name, cik, org in cur.fetchall():
        for n in {norm(name), key(sec_name)}:
            by_cik.add((n, (cik or "").lstrip("0")))
            by_org.add((n, (org or "").lower()))
            spelling.setdefault(n, name)
    log(f"SEC_FORM_345 insider-company keys: {len(by_cik):,}; insiders: {len(spelling):,}")

    cur.execute("SELECT id, source_data, source_name, target_id, target_name, relation_type, evidence "
                "FROM relationships WHERE source_data = ANY(%s)", (SOURCES,))
    old = cur.fetchall()
    flipped = {r[0]: si.flip_name(r[2] or "") for r in old}
    twins = existing(cur, {(flipped[r[0]], r[4], r[5]) for r in old if flipped[r[0]] != r[2]})
    delete, renames, kept = [], [], collections.defaultdict(list)
    why = collections.Counter()
    for rid, src, name, tid, org, rt, ev in old:
        f, k, cik = flipped[rid], key(name), cik_of(tid, ev)
        issuer = (org or "").lower()
        if src == "RECONCILED_FORM4":
            try:
                issuer = (json.loads(ev or "{}").get("issuer") or org or "").lower()
            except ValueError:
                pass
        if cik and (k, cik) in by_cik:
            reason = "same insider + issuer CIK in SEC_FORM_345"
        elif (k, issuer) in by_org or (k, (org or "").lower()) in by_org:
            reason = "same insider + company name in SEC_FORM_345"
        elif f != name and (f, org, rt) in twins:
            reason = "flipped row already exists (any source)"
        elif si.is_entity(name or "") or f == name:
            kept[src].append((name, org, rt))
            continue
        else:
            renames.append({"id": rid, "src": src, "old": name, "new": spelling.get(k, f), "org": org, "rt": rt,
                            "known": k in spelling})
            continue
        delete.append(rid)
        why[(src, reason)] += 1

    # a rename that would hit an existing row (unique index) or another rename
    # is a duplicate after all: delete it instead
    new_keys = collections.Counter((r["new"], r["org"], r["rt"]) for r in renames)
    clash = existing(cur, set(new_keys))
    seen, keep_renames = set(), []
    for r in renames:
        t = (r["new"], r["org"], r["rt"])
        if t in clash or t in seen:
            delete.append(r["id"])
            why[(r["src"], "renamed row would duplicate an existing one")] += 1
        else:
            seen.add(t)
            keep_renames.append(r)
    return old, delete, keep_renames, kept, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    conn = connect(readonly=not args.commit)
    cur = conn.cursor()
    old, delete, renames, kept, why = plan(cur)
    by_src = collections.Counter(r[1] for r in old)
    for s in SOURCES:
        n_del = sum(v for (src, _), v in why.items() if src == s)
        rs = [r for r in renames if r["src"] == s]
        log(f"{s}: {by_src[s]:,} rows; delete {n_del:,}; rename {len(rs):,} "
            f"({sum(r['known'] for r in rs):,} to an existing SEC_FORM_345 spelling); keep {len(kept[s]):,}")
        for (src, reason), v in sorted(why.items()):
            if src == s:
                log(f"    delete -- {reason}: {v:,}")
        for r in random.Random(4).sample(rs, min(6, len(rs))):
            log(f"    rename e.g. {r['old']!r} -> {r['new']!r} | {r['rt']} | {r['org']}")
        for k in random.Random(3).sample(kept[s], min(4, len(kept[s]))):
            log(f"    keep e.g. {k[0]} | {k[2]} | {k[1]}")
    if not args.commit:
        log("dry run -- nothing changed. Pass --commit to back up, delete and rename.")
        return
    cur.execute("SELECT to_regclass(%s)", (BACKUP,))
    if cur.fetchone()[0]:
        sys.exit(f"{BACKUP} already exists -- not overwriting it")
    affected = delete + [r["id"] for r in renames]
    cur.execute(f"CREATE TABLE {BACKUP} AS SELECT * FROM relationships WHERE id = ANY(%s) AND source_data = ANY(%s)",
                (affected, SOURCES))
    cur.execute(f"SELECT count(*) FROM {BACKUP}")
    backed = cur.fetchone()[0]
    if backed != len(affected):
        conn.rollback()
        sys.exit(f"backup has {backed:,} rows, expected {len(affected):,} -- rolled back")
    cur.execute("DELETE FROM relationships WHERE id = ANY(%s) AND source_data = ANY(%s)", (delete, SOURCES))
    deleted = cur.rowcount
    if deleted != len(delete):
        conn.rollback()
        sys.exit(f"would delete {deleted:,} rows, expected {len(delete):,} -- rolled back")
    from psycopg2.extras import execute_values
    execute_values(cur, "UPDATE relationships r SET source_name = v.name FROM (VALUES %s) AS v(id, name) "
                        "WHERE r.id = v.id AND r.source_data IN ('SEC_FORM_4', 'RECONCILED_FORM4')",
                   [(r["id"], r["new"]) for r in renames], page_size=max(1, len(renames)))
    renamed = cur.rowcount
    if renamed != len(renames):
        conn.rollback()
        sys.exit(f"renamed {renamed:,} rows, expected {len(renames):,} -- rolled back")
    conn.commit()
    log(f"backed up {backed:,} rows to {BACKUP}; deleted {deleted:,}, renamed {renamed:,}")
    conn.close()


if __name__ == "__main__":
    main()
