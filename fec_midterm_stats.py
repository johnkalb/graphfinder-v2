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

Super PACs (added 2026-10-14): committee types O (super PAC), V/W (hybrid
PACs' non-contribution accounts) and U (single-candidate IE committees).
  * spending: independent_expenditure_2026.csv (FEC, updated daily), House and
    Senate races only. Spending that supports a Democrat, or opposes a
    Republican in a general election, helps Democrats, and vice versa; in a
    primary, opposing a Democrat counts for the Democrats (a rival). Filers that aren't registered
    super PACs are dropped (the file has junk: "TANKING", $12 billion).
    Filings superseded by an amendment (another filing's prev_file_num) are
    dropped, and identical rows (spender, candidate, amount, date, purpose)
    repeated in a different filing -- a 24/48-hour report re-reported in a
    periodic one -- are counted once.
  * a super PAC's side is the side that gets 80%+ of its spending
  * money in: itemized receipts from individuals and organizations
    (corporations, unions, LLCs, nonprofits); committee-to-committee transfers
    are left out so money passed between super PACs isn't counted twice

  FEC_BULK_DIR=~/fec/bulk python fec_midterm_stats.py   -> fec_midterm_2026.json
"""
import csv
import json
import re
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
SUPER_PAC_TYPES = {"O", "V", "W", "U"}
SUPER_PAC_GIFT_TYPES = GIFT_TYPES | {"10"}       # 10 = contribution to an IE-only committee
SIDE_SHARE = 0.8
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
    party, kind, types = {}, {}, {}
    for r in rows_of(bulk("cm")):
        cmte, dsgn, tp, aff, cand = r[0], r[8], r[9], r[10], r[14] if len(r) > 14 else ""
        types[cmte] = tp
        if tp in ("X", "Y"):                         # party committees
            p = PARTY.get(aff)
            if p:
                party[cmte], kind[cmte] = p, "party"
        elif tp in ("H", "S") and dsgn in ("P", "A"):
            ids = ({cand} if cand else set()) | linked.get(cmte, set())
            ps = {cands[c][0] for c in ids if c in cands and cands[c][1] in ("H", "S")}
            if len(ps) == 1 and None not in ps:
                party[cmte], kind[cmte] = ps.pop(), "campaign"
    return party, kind, types, cands


def title(name):
    """FEC names arrive in any case; ALL-CAPS ones read as shouting on the ticker."""
    n = re.split(r",? (?:Inc\.?|INC\.?)(?:,| \(| DBA|$)", name.strip())[0].strip()
    if n.isupper():
        # acronyms stay: no vowels (CLF, HMP, SMP), in parentheses (UDP), or known
        n = " ".join(w if w in ("PAC", "USA", "LCV", "AB", "AFP", "SLF", "MAGA") or w.startswith("(")
                     or not re.search(r"[AEIOUY]", w) or not w.isalpha() and len(w) <= 4
                     else w.lower() if i and w in ("FOR", "A", "OF", "THE", "AND", "TO", "IN")
                     else w.capitalize() for i, w in enumerate(n.split()))
        n = re.sub(r"^MAGA$", "MAGA Inc.", n)
    # registered under acronyms the press spells out
    n = {"CLF": "Congressional Leadership Fund", "HMP": "House Majority PAC", "SMP": "Senate Majority PAC",
         "SLF PAC": "Senate Leadership Fund"}.get(n, n)
    return re.sub(r"\bPac\b", "PAC", n)


IE_PARTY = {"DEMOCRATIC PARTY": "D", "DEMOCRATIC-FARMER-LABOR": "D", "REPUBLICAN PARTY": "R"}


def independent_expenditures(types, cands):
    """Super PAC spending on House/Senate races, deduplicated (see docstring)."""
    path = os.path.join(BULK, str(YEAR), f"independent_expenditure_{YEAR}.csv")
    with open(path, encoding="latin-1") as f:
        rows = [r for r in csv.DictReader(f)
                if types.get(r["spe_id"]) in SUPER_PAC_TYPES and r["can_office"] in ("H", "S")]
    superseded = {r["prev_file_num"] for r in rows if r["prev_file_num"]}
    seen, kept = {}, []
    for r in rows:
        if r["file_num"] in superseded:
            continue
        k = (r["spe_id"], r["cand_id"], r["exp_amo"], r["dissem_dt"] or r["exp_date"], r["pur"])
        if seen.setdefault(k, r["file_num"]) != r["file_num"]:
            continue                     # same expenditure, already counted from another filing
        kept.append(r)
    log(f"IE: {len(rows):,} super PAC rows on H/S races, {len(kept):,} after dedup")
    # some filers (CLF: $103M) name the candidate but leave cand_id blank --
    # look the party up by office + state + district + last name
    by_race_name = defaultdict(set)
    for r in rows_of(bulk("cn")):
        if PARTY.get(r[2]):
            dist = r[6] if r[5] == "H" else ""
            by_race_name[(r[5], r[4], dist, r[1].split(",")[0].strip().upper())].add(PARTY[r[2]])

    def party_by_name(r):
        dist = r["can_office_dis"].zfill(2) if r["can_office"] == "H" else ""
        ps = by_race_name.get((r["can_office"], r["can_office_state"], dist,
                               r["cand_name"].split(",")[0].strip().upper()), set())
        return next(iter(ps)) if len(ps) == 1 else None
    by_side, attack = Counter(), Counter()
    spender = defaultdict(Counter)       # spe_id -> side -> $
    names, race = {}, defaultdict(Counter)
    for r in kept:
        try:
            amt = float(r["exp_amo"])
        except ValueError:
            continue
        cand_party = (cands.get(r["cand_id"], (None, None))[0] or IE_PARTY.get(r["cand_pty_aff"])
                      or party_by_name(r))
        so = r["sup_opp"]
        if cand_party not in ("D", "R") or so not in ("S", "O"):
            side = "other"
        else:
            # attacking a candidate helps the other party only in a general
            # election; in a primary or runoff it helps a rival in the same party
            general = r["ele_type"] in ("G", "S")
            side = cand_party if so == "S" or not general else ("R" if cand_party == "D" else "D")
        by_side[side] += amt
        if so == "O":
            attack[side] += amt
        spender[r["spe_id"]][side] += amt
        names[r["spe_id"]] = r["spe_nam"]
        office = r["can_office"]
        race[(office, r["can_office_state"], r["can_office_dis"] if office == "H" else "")][side] += amt

    def side_label(c):
        tot = sum(c.values())
        for s in ("D", "R"):
            if tot and c[s] / tot >= SIDE_SHARE:
                return s
        return "mixed"
    sides = {sid: side_label(c) for sid, c in spender.items()}
    top = sorted(spender, key=lambda s: -sum(spender[s].values()))[:10]
    races = sorted(race, key=lambda k: -sum(race[k].values()))[:10]
    return {
        "total": sum(by_side.values()), "by_side": dict(by_side), "attack": dict(attack),
        "spenders": len(spender),
        "top_spenders": [{"id": s, "name": title(names[s]), "spent": sum(spender[s].values()), "side": sides[s]}
                         for s in top],
        "top_races": [{"office": o, "state": st, "district": dis, "spent": sum(race[(o, st, dis)].values()),
                       "by_side": dict(race[(o, st, dis)])} for o, st, dis in races],
        "file_date": time.strftime("%Y-%m-%d", time.gmtime(os.path.getmtime(path))),
    }, sides


def donor_key(r):
    name = person_name(r[7])
    if not name:
        return None
    toks = name.split()
    suf = toks[-1] if toks[-1].upper() in _SUFFIXES and len(toks) > 2 else ""
    core = toks[:-1] if suf else toks
    return canon_key(" ".join([core[0], core[-1], suf]).strip()) + "|" + r[10][:5]


def fec_totals(party, kind):
    """Individual contributions incl. unitemized, and contributions from PACs
    and other political committees (FEC doesn't split those), from FEC's
    summary files."""
    tot, pac = Counter(), Counter()
    ends = []
    for r in rows_of(bulk("webl")):                  # House/Senate current campaigns
        p = PARTY.get(r[4])
        if p and r[0][:1] in ("H", "S"):
            tot[p] += float(r[17] or 0)
            pac[p] += float(r[25] or 0)              # OTHER_POL_CMTE_CONTRIB
            if r[27]:
                ends.append(r[27][6:] + r[27][:2] + r[27][3:5])
    for r in rows_of(bulk("webk")):                  # party committees
        if kind.get(r[0]) == "party":
            tot[party[r[0]]] += float(r[7] or 0)
            pac[party[r[0]]] += float(r[8] or 0)     # OTHER_POL_CMTE_CONTRIB
            if r[-1]:
                ends.append(r[-1][6:] + r[-1][:2] + r[-1][3:5])
    return tot, pac, (sorted(ends)[len(ends) // 2] if ends else None)


def main():
    t = time.time()
    party, kind, ctypes, cands = committee_parties()
    log(f"in-scope committees: {Counter(party.values())} ({Counter(kind.values())})")
    ie, sp_side = independent_expenditures(ctypes, cands)
    sp_gave = Counter()                              # super PAC donor -> total
    sp_side_gave = {"D": Counter(), "R": Counter()}
    sp_orgs = 0.0

    gave = {"D": Counter(), "R": Counter()}          # donor -> total to that party
    gifts = {"D": [], "R": []}                       # every gift amount
    skipped, types, latest = Counter(), Counter(), ""
    for r in rows_of(bulk("indiv")):
        if len(r) < 19:
            continue
        if ctypes.get(r[0]) in SUPER_PAC_TYPES:
            if (r[18] != "X" and r[5] in SUPER_PAC_GIFT_TYPES and r[13][4:] in ("2025", "2026")
                    and r[6] in ("IND", "ORG")):
                try:
                    amt = float(r[14])
                except ValueError:
                    amt = 0
                k = donor_key(r) if r[6] == "IND" else "org|" + canon_key(r[7])
                if amt > 0 and k:
                    sp_gave[k] += amt
                    if r[6] == "ORG":
                        sp_orgs += amt
                    if sp_side.get(r[0]) in sp_side_gave:
                        sp_side_gave[sp_side[r[0]]][k] += amt
            continue
        if r[6] != "IND":
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

    fec_tot, fec_pac, coverage = fec_totals(party, kind)
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
            "fec_individual_total": fec_tot[p], "fec_committee_total": fec_pac[p],
            "median_gift": statistics.median(gifts[p]), "median_donor_total": statistics.median(totals),
            "top1pct_donors": len(top1), "top1pct_dollar_share": sum(top1) / dollars,
            "buckets": buckets,
        }
    both = gave["D"].keys() & gave["R"].keys()
    out["both"] = {"donors": len(both),
                   "to_D": sum(gave["D"][k] for k in both), "to_R": sum(gave["R"][k] for k in both)}
    def concentration(gave):
        vals = sorted(gave.values(), reverse=True)
        total = sum(vals)
        mil = [v for v in vals if v >= 1_000_000]
        return {"donors": len(vals), "dollars": total, "million_donors": len(mil),
                "million_share": sum(mil) / total if total else 0,
                "top100_share": sum(vals[:100]) / total if total else 0}
    out["superpacs"] = {"spending": ie, "receipts": concentration(sp_gave), "receipts_from_orgs": sp_orgs,
                        "receipts_by_side": {s: concentration(g) for s, g in sp_side_gave.items()}}
    with open(OUT, "w") as f:
        json.dump(out, f, indent=1)
    log(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
