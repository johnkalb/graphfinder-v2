"""FEC committee spending: who campaigns and PACs pay (Phase 3 of the FEC plan,
2026-10-03), cycles 2008-2026.

oppexp{yy}.zip operating expenditures: CMTE_ID(0) ... NAME(8) payee, CITY(9)
STATE(10) ... TRANSACTION_AMT(13) ... PURPOSE(15) ... MEMO_CD(18) ...
ENTITY_TP(20) ...

Kept: payments to organizations (ENTITY_TP ORG/COM/PAC/PTY/CCM -- payments to
individuals are mostly staff pay, private people), memo lines skipped,
aggregated per (committee, payee) over all cycles, $10,000+ in total.
Dropped: payees paid by more than MAX_PAYERS different committees -- the
Postal Service, Facebook, banks, payment processors. They'd connect every
campaign to every other through one node, which says nothing about who works
with whom; consultants, pollsters and media buyers serve far fewer.

  committee  PAID_VENDOR  payee     source_data='FEC_SPEND' (reversible)

  python fec_spending_backfill.py            # aggregate -> data/fec_spend_rows.jsonl.gz, print counts
  python fec_spending_backfill.py --commit   # load into Postgres
"""
import gzip
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

from fec_structure_backfill import CYCLES, fetch, log, rows_of

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "FEC_SPEND"
OUT = os.path.join(HERE, "data", "fec_spend_rows.jsonl.gz")
MIN_TOTAL = 10_000
MAX_PAYERS = 250
ORG_TYPES = {"ORG", "COM", "PAC", "PTY", "CCM"}


def aggregate():
    with gzip.open(os.path.join(HERE, "data", "fec_committee_names.json.gz"), "rt", encoding="utf-8") as f:
        cmte = json.load(f)
    agg = defaultdict(lambda: [0.0, 0, set(), Counter(), ""])   # (cmte_id, payee key) -> [$, n, cycles, purposes, display]
    for year in CYCLES:
        t = time.time()
        n = 0
        for r in rows_of(fetch("oppexp", year)):
            if len(r) < 21 or r[18] == "X" or r[20] not in ORG_TYPES:
                continue
            try:
                amt = float(r[13])
            except ValueError:
                continue
            payee = re.sub(r"\s+", " ", r[8]).strip(" .,")
            if amt <= 0 or not payee or r[0] not in cmte:
                continue
            a = agg[(r[0], payee.upper())]
            a[0] += amt; a[1] += 1; a[2].add(year)
            if r[15]:
                a[3][r[15].strip()[:60]] += 1
            a[4] = a[4] or payee
            n += 1
        log(f"  {year}: {n:,} payments ({time.time() - t:.0f}s); {len(agg):,} pairs so far")
    payers = Counter(p for (_, p) in agg)
    rows, dropped_hubs = [], Counter()
    for (cid, pkey), (amt, n, cycles, purposes, display) in agg.items():
        if amt < MIN_TOTAL:
            continue
        if payers[pkey] > MAX_PAYERS:
            dropped_hubs[display] += 1
            continue
        rows.append([cmte[cid], display, round(amt, 2), n, sorted(cycles), (purposes.most_common(1) or [("", 0)])[0][0]])
    return rows, dropped_hubs


def main():
    if "--commit" not in sys.argv:
        rows, hubs = aggregate()
        with gzip.open(OUT, "wt", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        log(f"{len(rows):,} committee->vendor rows (${sum(r[2] for r in rows) / 1e9:,.2f}B); "
            f"{len(hubs):,} ubiquitous payees dropped, e.g. {', '.join(h for h, _ in hubs.most_common(12))}")
        for r in sorted(rows, key=lambda r: -r[2])[:10]:
            log(f"  ${r[2] / 1e6:,.1f}M  {r[0]} -> {r[1]}  ({r[5]})")
        log(f"wrote {OUT} -- run with --commit to load")
        return
    import psycopg2
    from psycopg2.extras import execute_values
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import pg_secret
    out = []
    with gzip.open(OUT, "rt", encoding="utf-8") as f:
        for line in f:
            cname, payee, amt, n, cycles, purpose = json.loads(line)
            ev = json.dumps({"source": SOURCE, "total_usd": amt, "payments": n, "cycles": cycles, "purpose": purpose},
                            separators=(",", ":"))
            out.append((None, cname, "ORG", None, payee, "ORG", "PAID_VENDOR", SOURCE, ev))
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   out, page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")


if __name__ == "__main__":
    main()
