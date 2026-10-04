"""FEC PAC money flows (Phase 1 of the FEC plan, 2026-10-03), cycles 2008-2026.

  pas2{yy}.zip  committee -> candidate: contributions (24K, 24Z in-kind) and
                independent expenditures for (24E) / against (24A) a candidate
  oth{yy}.zip   committee <-> committee transfers; RECEIPTS only (filer is
                the recipient, OTHER_ID the giver) so each transfer counts once

Both files share the itcont column layout: CMTE_ID(0) ... TRANSACTION_TP(5)
ENTITY_TP(6) NAME(7) ... TRANSACTION_DT(13) TRANSACTION_AMT(14) OTHER_ID(15)
CAND_ID(16) ... MEMO_CD(19). Memo lines (MEMO_CD 'X') are skipped -- they
itemise money already reported on another line.

One row per (giver, recipient, relation), aggregated over all cycles, with
total dollars, transaction count and the cycles seen in the evidence:
  committee  PAC_CONTRIBUTION                 recipient committee (Phase 0 links it to the candidate)
  committee  INDEPENDENT_EXPENDITURE_FOR      candidate
  committee  INDEPENDENT_EXPENDITURE_AGAINST  candidate   (kept, but not scored -- build_scored_edges drops it)
  committee  COMMITTEE_TRANSFER               committee
source_data='FEC_PAC' (reversible: DELETE ... WHERE source_data='FEC_PAC').

  python fec_pac_backfill.py            # download, aggregate, write data/fec_pac_rows.jsonl.gz, print counts
  python fec_pac_backfill.py --commit   # load that file into Postgres (no re-download)

The aggregation pass reads ~4 GB of zips; run it from Task Scheduler, not an
interactive shell (see memory: long pipeline jobs).
"""
import gzip
import json
import os
import sys
import time
from collections import defaultdict

from fec_structure_backfill import BULK, CYCLES, fetch, log, person_name, rows_of, wikidata_fec_labels

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "FEC_PAC"
OUT = os.path.join(HERE, "data", "fec_pac_rows.jsonl.gz")
PAS2_REL = {"24K": "PAC_CONTRIBUTION", "24Z": "PAC_CONTRIBUTION",
            "24E": "INDEPENDENT_EXPENDITURE_FOR", "24A": "INDEPENDENT_EXPENDITURE_AGAINST"}
CONDUITS = {"C00401224", "C00694323"}     # ActBlue, WinRed: pass-through, not a real counterparty


def _amount(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def aggregate():
    with gzip.open(os.path.join(HERE, "data", "fec_committee_names.json.gz"), "rt", encoding="utf-8") as f:
        cmte = json.load(f)
    wd = wikidata_fec_labels()
    cand = {}
    for year in CYCLES:
        for r in rows_of(fetch("cn", year)):
            if len(r) > 1 and r[0]:
                cand[r[0]] = wd.get(r[0]) or person_name(r[1]) or cand.get(r[0])
    agg = defaultdict(lambda: [0.0, 0, set()])   # (giver, recipient, rel, recipient_type) -> [$, n, cycles]
    for year in CYCLES:
        t = time.time()
        n = 0
        for r in rows_of(fetch("pas2", year)):
            if len(r) < 20 or r[19] == "X" or r[5] not in PAS2_REL:
                continue
            amt = _amount(r[14])
            if amt <= 0:
                continue
            rel = PAS2_REL[r[5]]
            giver = cmte.get(r[0])
            if rel == "PAC_CONTRIBUTION":
                target, ttype = cmte.get(r[15]), "ORG"
            else:
                target, ttype = cand.get(r[16]), "PERSON"
            if giver and target and r[0] not in CONDUITS:
                a = agg[(giver, target, rel, ttype)]
                a[0] += amt; a[1] += 1; a[2].add(year); n += 1
        log(f"  pas2 {year}: {n:,} transactions ({time.time() - t:.0f}s)")
        t = time.time()
        n = 0
        for r in rows_of(fetch("oth", year)):
            # receipts (TRANSACTION_TP 1x) from another committee (OTHER_ID C...)
            if len(r) < 20 or r[19] == "X" or not r[5].startswith("1") or not r[15].startswith("C"):
                continue
            if r[0] in CONDUITS or r[15] in CONDUITS:
                continue
            amt = _amount(r[14])
            giver, target = cmte.get(r[15]), cmte.get(r[0])
            if amt <= 0 or not giver or not target or giver == target:
                continue
            a = agg[(giver, target, "COMMITTEE_TRANSFER", "ORG")]
            a[0] += amt; a[1] += 1; a[2].add(year); n += 1
        log(f"  oth  {year}: {n:,} transfers ({time.time() - t:.0f}s); {len(agg):,} pairs so far")
    return agg


def main():
    if "--commit" not in sys.argv:
        agg = aggregate()
        with gzip.open(OUT, "wt", encoding="utf-8") as f:
            for (giver, target, rel, ttype), (amt, n, cycles) in agg.items():
                f.write(json.dumps([giver, target, ttype, rel, round(amt, 2), n, sorted(cycles)]) + "\n")
        by_rel = defaultdict(lambda: [0, 0.0])
        for (_, _, rel, _), (amt, _, _) in agg.items():
            by_rel[rel][0] += 1; by_rel[rel][1] += amt
        for rel, (k, amt) in sorted(by_rel.items(), key=lambda kv: -kv[1][0]):
            log(f"{rel:32s} {k:>9,} pairs  ${amt / 1e9:,.2f}B")
        top = sorted(agg.items(), key=lambda kv: -kv[1][0])[:10]
        for (g, t, rel, _), (amt, n, cyc) in top:
            log(f"  ${amt / 1e6:,.1f}M  {g} -{rel}-> {t}")
        log(f"wrote {OUT} -- run with --commit to load")
        return
    import psycopg2
    from psycopg2.extras import execute_values
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import pg_secret
    rows = []
    with gzip.open(OUT, "rt", encoding="utf-8") as f:
        for line in f:
            giver, target, ttype, rel, amt, n, cycles = json.loads(line)
            ev = json.dumps({"source": SOURCE, "total_usd": amt, "transactions": n, "cycles": cycles},
                            separators=(",", ":"))
            rows.append((None, giver, "ORG", None, target, ttype, rel, SOURCE, ev))
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   rows, page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")


if __name__ == "__main__":
    main()
