"""FEC individual contributions, re-harvested correctly (Phase 2 of the FEC plan, 2026-10-03).

The old harvester (fec-contributions agent) took OTHER_ID as the recipient and
skipped every row without one, so it kept only earmarked (ActBlue/WinRed)
donations and lost every direct one. Here the recipient is the filing
committee (CMTE_ID), the committee that received the money. Earmarked gifts
are counted once, on the receiving candidate's report (15E rows); rows filed
by the conduits themselves (ActBlue/WinRed) are skipped. Memo lines (MEMO_CD
'X') and refunds/negative amounts are skipped too.

Who is kept, per cycle (2008-2026):
  * every donor (individual or organization) giving $10,000+ in total that cycle
  * any donor whose name is a verified public figure (verified_people.json.gz),
    whatever the amount. Not every graph person: a 2012 test kept 464K
    small donors that way, mostly ordinary people sharing a name with
    someone in the graph.
A donor is identified by name + 5-digit ZIP (individuals) or name
(organizations). One row per donor-committee pair per cycle, with the total.

indiv{yy}.zip columns: CMTE_ID(0) AMNDT_IND RPT_TP TRANSACTION_PGI IMAGE_NUM
TRANSACTION_TP(5) ENTITY_TP(6) NAME(7) CITY(8) STATE(9) ZIP_CODE(10)
EMPLOYER(11) OCCUPATION(12) TRANSACTION_DT(13) TRANSACTION_AMT(14) OTHER_ID(15)
TRAN_ID(16) FILE_NUM(17) MEMO_CD(18) MEMO_TEXT(19) SUB_ID(20)

  python fec_individual_harvest.py 2012 [2014 ...]   # aggregate cycles -> data/fec_indiv_{year}.jsonl.gz
  python fec_individual_harvest.py --commit           # load every cycle file into Postgres
source_data='FEC_INDIV' (reversible). Runs on optiplex (big downloads:
~4-6 GB per recent cycle); each cycle is two passes over its zip, so a cycle
whose output exists is skipped on rerun.
"""
import gzip
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict

from fec_structure_backfill import BULK, CYCLES, fetch, log, person_name, rows_of

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = "FEC_INDIV"
THRESHOLD = 10_000
_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}
CONDUITS = {"C00401224", "C00694323"}     # ActBlue, WinRed
VERIFIED = os.environ.get("VERIFIED_PATH", os.path.join(HERE, "data", "verified_people.json.gz"))
_COMBINING = dict.fromkeys(range(0x300, 0x370))


def canon_key(name):
    """Same as build_scored_edges.canon_key."""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = re.sub(r"\s+", " ", re.sub(r"[^\w ]|_", "", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


def out_path(year):
    return os.path.join(HERE, "data", f"fec_indiv_{year}.jsonl.gz")


def donations(year):
    """(donor_key, display_name, entity_type, cmte_id, amount, row) for countable receipts."""
    for r in rows_of(fetch("indiv", year)):
        if len(r) < 19 or r[18] == "X" or r[0] in CONDUITS:
            continue
        try:
            amt = float(r[14])
        except ValueError:
            continue
        if amt <= 0:
            continue
        ent = r[6]
        if ent == "IND":
            name = person_name(r[7])
            if not name:
                continue
            # identity: first + last (+ suffix) + ZIP5, ignoring middle names --
            # "Sheldon Adelson" and "Sheldon G Adelson" at 89109 are one donor
            toks = name.split()
            suf = toks[-1] if toks[-1].upper() in _SUFFIXES and len(toks) > 2 else ""
            core = toks[:-1] if suf else toks
            key = (canon_key(" ".join([core[0], core[-1], suf]).strip()), r[10][:5])
            full = canon_key(name)
        elif ent in ("ORG", "COM", "PAC", "PTY", "CCM"):
            name = re.sub(r"\s+", " ", r[7]).strip()
            if not name:
                continue
            key = (canon_key(name), "")
            full = key[0]
        else:
            continue
        yield key, full, name, ent, r[0], amt, r


def harvest(year, graph_people):
    t = time.time()
    totals = Counter()
    in_graph = set()
    for key, full, _, _, _, amt, _ in donations(year):
        totals[key] += amt
        if key[0] in graph_people or full in graph_people:
            in_graph.add(key)
    keep = {k for k, v in totals.items() if v >= THRESHOLD} | in_graph
    log(f"  {year} pass 1: {len(totals):,} donors, {len(keep):,} kept "
        f"({sum(1 for k in keep if totals[k] >= THRESHOLD):,} at ${THRESHOLD:,}+) ({time.time() - t:.0f}s)")
    del totals
    t = time.time()
    agg = {}
    names = defaultdict(Counter)     # donor key -> spellings, so every row of a donor uses one name
    for key, _, name, ent, cmte, amt, r in donations(year):
        if key not in keep:
            continue
        names[key][name] += 1
        a = agg.get((key, cmte))
        if a is None:
            a = agg[(key, cmte)] = {"name": name, "ent": ent, "cmte": cmte, "usd": 0.0, "n": 0,
                                    "emp": Counter(), "occ": Counter(), "city": r[8], "state": r[9], "zip": key[1]}
        a["usd"] += amt
        a["n"] += 1
        if r[11]:
            a["emp"][r[11]] += 1
        if r[12]:
            a["occ"][r[12]] += 1
    with gzip.open(out_path(year) + ".tmp", "wt", encoding="utf-8") as f:
        for (key, _), a in agg.items():
            f.write(json.dumps({"name": names[key].most_common(1)[0][0], "dkey": f"{key[0]}|{key[1]}", "ent": a["ent"], "cmte": a["cmte"], "usd": round(a["usd"], 2),
                                "n": a["n"], "employer": (a["emp"].most_common(1) or [("", 0)])[0][0],
                                "occupation": (a["occ"].most_common(1) or [("", 0)])[0][0],
                                "city": a["city"], "state": a["state"], "zip": a["zip"], "cycle": year}) + "\n")
    os.replace(out_path(year) + ".tmp", out_path(year))
    log(f"  {year} pass 2: {len(agg):,} donor-committee rows written ({time.time() - t:.0f}s)")


def commit():
    """Load every cycle file. The table is unique on (source, target, type), so
    rows are merged per (donor name, committee) across cycles first: totals
    summed, cycles listed, the most common employer/occupation/place kept."""
    import psycopg2
    from psycopg2.extras import execute_values
    with gzip.open(os.path.join(HERE, "data", "fec_committee_names.json.gz"), "rt", encoding="utf-8") as f:
        cmte_name = json.load(f)
    merged = {}
    spellings = defaultdict(Counter)
    for year in CYCLES:
        if not os.path.exists(out_path(year)):
            log(f"  {year}: no file, skipped")
            continue
        with gzip.open(out_path(year), "rt", encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                target = cmte_name.get(d["cmte"])
                if not target:
                    continue
                spellings[d["dkey"]][d["name"]] += d["n"]
                k = (d["dkey"], target.lower())
                m = merged.get(k)
                if m is None:
                    m = merged[k] = {"dkey": d["dkey"], "ent": d["ent"], "target": target, "cmte": d["cmte"],
                                     "usd": 0.0, "n": 0, "cycles": set(), "emp": Counter(), "occ": Counter(),
                                     "place": Counter()}
                m["usd"] += d["usd"]; m["n"] += d["n"]; m["cycles"].add(d["cycle"])
                if d["employer"]:
                    m["emp"][d["employer"]] += d["n"]
                if d["occupation"]:
                    m["occ"][d["occupation"]] += d["n"]
                m["place"][(d["city"], d["state"], d["zip"])] += d["n"]
    top = lambda c: (c.most_common(1) or [("", 0)])[0][0]
    # The table is unique on (name, committee, type): donors who share a
    # display name (different ZIPs) are combined here; the graph build splits
    # common names again by employer.
    final = {}
    for m in merged.values():
        name = top(spellings[m["dkey"]])
        k = (name.lower(), m["target"].lower())
        if k in final:
            f_ = final[k]
            f_["usd"] += m["usd"]; f_["n"] += m["n"]; f_["cycles"] |= m["cycles"]
            f_["emp"].update(m["emp"]); f_["occ"].update(m["occ"]); f_["place"].update(m["place"])
        else:
            m["name"] = name
            final[k] = m
    rows = []
    for m in final.values():
        city, state, zip5 = top(m["place"]) or ("", "", "")
        ev = {"source": SOURCE, "total_usd": round(m["usd"], 2), "contributions": m["n"],
              "cycles": sorted(m["cycles"]), "recipient": m["cmte"], "employer": top(m["emp"]),
              "occupation": top(m["occ"]), "city": city, "state": state, "zip": zip5}
        rows.append((None, m["name"], "PERSON" if m["ent"] == "IND" else "ORG", None, m["target"], "ORG",
                     "DONATION", SOURCE, json.dumps(ev, separators=(",", ":"))))
    log(f"{len(rows):,} donor-committee rows after merging cycles")
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, "
                        "target_name, target_type, relation_type, source_data, evidence) VALUES %s "
                        "ON CONFLICT DO NOTHING", rows, page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")


def main():
    if "--commit" in sys.argv:
        commit()
        return
    with gzip.open(VERIFIED, "rt", encoding="utf-8") as f:
        graph_people = {canon_key(n) for n in json.load(f)}
    log(f"{len(graph_people):,} verified public figures")
    years = [int(a) for a in sys.argv[1:]] or CYCLES
    for year in years:
        if os.path.exists(out_path(year)):
            log(f"  {year}: already done")
            continue
        harvest(year, graph_people)
        zpath = os.path.join(BULK, str(year), f"indiv{str(year)[2:]}.zip")
        if os.path.exists(zpath):
            os.remove(zpath)      # 4-6 GB each; the aggregate is what we keep


if __name__ == "__main__":
    main()
