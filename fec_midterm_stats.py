"""2026 midterm money by party, for the crawlie ticker (2026-10-14).

Who gave to Democrats and Republicans this cycle, how much, and how the gifts
are distributed. Reads the FEC bulk files directly (not Postgres -- FEC_INDIV
there keeps only $10K+ donors, so it can't count small ones):

  indiv26  itemized individual contributions
  cm26/cn26/ccl26  committee -> party (via its candidate for campaign committees)
  webl26/webk26  FEC's own per-committee totals, which include unitemized gifts

Scope: House and Senate campaign committees (principal/authorized) of
Democratic and Republican candidates, plus the parties' own committees
(DNC/RNC, DCCC/NRCC, DSCC/NRSC, state parties' federal accounts). Super PACs
and other PACs declare no party and are left out; presidential committees are
left out (not the midterms).

Counting rules, matching fec_individual_harvest.py:
  * rows filed by the conduits ActBlue/WinRed are skipped; the gift is counted
    on the receiving committee's 15E row (recipients itemize far more of them:
    7.3M ActBlue-earmarked 15E rows vs 975K ActBlue 24T rows in the 2026 file)
  * memo lines are skipped (in this file they are redesignations and
    reattributions of a gift already counted)
  * only contribution types count (GIFT_TYPES): 22Y etc. are refunds TO the
    donor, reported as positive amounts; non-positive amounts are skipped too
  * joint fundraising committees are out of scope (they declare no party), and
    their transfers reach campaigns as committee money, not as itemized
    individual rows -- so JFC-routed gifts are missing from the donor counts
  * a donor is first + last name (+ suffix) + 5-digit ZIP
  * only transactions dated 2025-2026
Committees itemize donors only above $200 a cycle (though most earmarked small
gifts are itemized), so donor counts are counts of ITEMIZED donors; the totals
from webl26/webk26 cover everyone.

  FEC_BULK_DIR=~/fec/bulk python fec_midterm_stats.py   -> fec_midterm_2026.json
"""
import json
import os
import statistics
import sys
import time
from collections import Counter, defaultdict

from fec_structure_backfill import BULK, log, person_name, rows_of
from fec_individual_harvest import CONDUITS, _SUFFIXES, canon_key

YEAR = 2026
OUT = sys.argv[1] if len(sys.argv) > 1 else "fec_midterm_2026.json"
PARTY = {"DEM": "D", "DFL": "D", "REP": "R"}
GIFT_TYPES = {"15", "15E", "15C", "11", "30", "30E", "31", "31E", "32", "32E"}
BUCKETS = [(0, 200), (200, 1000), (1000, 5000), (5000, 50_000), (50_000, float("inf"))]


def bulk(kind):
    return os.path.join(BULK, str(YEAR), f"{kind}{str(YEAR)[2:]}.zip")


def committee_parties():
    """cmte_id -> 'D'/'R' for in-scope committees, plus {cmte: kind}."""
    cands = {}                      # cand_id -> (party, office)
    for r in rows_of(bulk("cn")):
        cands[r[0]] = (PARTY.get(r[2]), r[5])
    linked = defaultdict(set)       # cmte -> cand_ids (ccl linkages)
    for r in rows_of(bulk("ccl")):
        if r[5] in ("P", "A"):
            linked[r[3]].add(r[0])
    party, kind = {}, {}
    for r in rows_of(bulk("cm")):
        cmte, dsgn, tp, aff, cand = r[0], r[8], r[9], r[10], r[14] if len(r) > 14 else ""
        if tp in ("X", "Y"):                         # party committees
            p = PARTY.get(aff)
            if p:
                party[cmte], kind[cmte] = p, "party"
        elif tp in ("H", "S") and dsgn in ("P", "A"):
            ids = ({cand} if cand else set()) | linked.get(cmte, set())
            ps = {cands[c][0] for c in ids if c in cands and cands[c][1] in ("H", "S")}
            if len(ps) == 1 and None not in ps:
                party[cmte], kind[cmte] = ps.pop(), "campaign"
    return party, kind


def donor_key(r):
    name = person_name(r[7])
    if not name:
        return None
    toks = name.split()
    suf = toks[-1] if toks[-1].upper() in _SUFFIXES and len(toks) > 2 else ""
    core = toks[:-1] if suf else toks
    return canon_key(" ".join([core[0], core[-1], suf]).strip()) + "|" + r[10][:5]


def fec_totals(party, kind):
    """Individual contributions incl. unitemized, from FEC's summary files."""
    tot = Counter()
    ends = []
    for r in rows_of(bulk("webl")):                  # House/Senate current campaigns
        p = PARTY.get(r[4])
        if p and r[0][:1] in ("H", "S"):
            tot[p] += float(r[17] or 0)
            if r[27]:
                ends.append(r[27][6:] + r[27][:2] + r[27][3:5])
    for r in rows_of(bulk("webk")):                  # party committees
        if kind.get(r[0]) == "party":
            tot[party[r[0]]] += float(r[7] or 0)
            if r[-1]:
                ends.append(r[-1][6:] + r[-1][:2] + r[-1][3:5])
    return tot, (sorted(ends)[len(ends) // 2] if ends else None)


def main():
    t = time.time()
    party, kind = committee_parties()
    log(f"in-scope committees: {Counter(party.values())} ({Counter(kind.values())})")

    gave = {"D": Counter(), "R": Counter()}          # donor -> total to that party
    gifts = {"D": [], "R": []}                       # every gift amount
    skipped, types, latest = Counter(), Counter(), ""
    for r in rows_of(bulk("indiv")):
        if len(r) < 19 or r[6] != "IND":
            continue
        p = party.get(r[0])
        if not p:
            continue
        if r[0] in CONDUITS:
            skipped["conduit"] += 1
            continue
        if r[18] == "X":
            skipped["memo"] += 1
            continue
        if r[5] not in GIFT_TYPES:
            skipped[f"type {r[5]}"] += 1
            continue
        d = r[13]
        if len(d) != 8 or d[4:] not in ("2025", "2026"):
            skipped["date"] += 1
            continue
        try:
            amt = float(r[14])
        except ValueError:
            continue
        if amt <= 0:
            skipped["refund"] += 1
            continue
        k = donor_key(r)
        if not k:
            skipped["name"] += 1
            continue
        gave[p][k] += amt
        gifts[p].append(amt)
        types[r[5]] += 1
        ymd = d[4:] + d[:4]
        if ymd > latest and ymd <= time.strftime("%Y%m%d"):
            latest = ymd
    log(f"read indiv in {time.time() - t:.0f}s; skipped {dict(skipped)}; types {types.most_common(8)}")

    fec_tot, coverage = fec_totals(party, kind)
    out = {"cycle": YEAR, "latest_gift": latest, "coverage_median_end": coverage,
           "fec_file": time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(bulk("indiv")))), "parties": {}}
    for p in ("D", "R"):
        totals = sorted(gave[p].values())
        dollars = sum(totals)
        n = len(totals)
        top1 = totals[-max(1, n // 100):]
        buckets = []
        for lo, hi in BUCKETS:
            sel = [v for v in totals if lo <= v < hi]
            buckets.append({"lo": lo, "hi": None if hi == float("inf") else hi,
                            "donors": len(sel), "donor_share": len(sel) / n, "dollars": sum(sel),
                            "dollar_share": sum(sel) / dollars})
        out["parties"][p] = {
            "donors": n, "gifts": len(gifts[p]), "itemized_dollars": dollars,
            "fec_individual_total": fec_tot[p],
            "median_gift": statistics.median(gifts[p]), "median_donor_total": statistics.median(totals),
            "top1pct_donors": len(top1), "top1pct_dollar_share": sum(top1) / dollars,
            "buckets": buckets,
        }
    both = gave["D"].keys() & gave["R"].keys()
    out["both"] = {"donors": len(both),
                   "to_D": sum(gave["D"][k] for k in both), "to_R": sum(gave["R"][k] for k in both)}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    log(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
