"""People who are public by their ROLE, not their fame (operator decision 2026-10-07).

"Verified public figure" used to mean a confirmed Wikidata identity (plus the
Wikidata imports and a short famous-names list). That missed people whose role
makes them public whether or not they're notable:
  candidate     federal candidates (FEC candidate -> committee links)
  legislator    members of Congress and state legislatures
  judge         judges (CourtListener people + financial disclosures)
  sec_insider   officers, directors and 10% owners filing SEC Form 4
  lobbyist      registered federal lobbyists (LDA)

Safeguards:
  * only names the graph's own person typing calls people (graph_people.tsv.gz,
    from build_person_nodes.py) -- drops funds and companies that file Form 4
    as reporting persons, and junk like "2008";
  * common names are left out: a name shared by FEC donors at 5+ different
    ZIP codes (data/fec_indiv_*.jsonl.gz) is several people, so "John Smith the
    lobbyist" mustn't make every John Smith public.

Writes ~/public_by_role.json ({name: [roles]}), which
build_crawlie_facts.load_verified_people() adds to the verified set (and so to
verified_people.json.gz for the demo, public links and query rules).
Runs in the nightly rebuild before build_crawlie_facts.py.
"""
import glob
import gzip
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
import pg_secret  # noqa: E402
import psycopg2  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.environ.get("PUBLIC_BY_ROLE_PATH", os.path.join(os.path.expanduser("~"), "public_by_role.json"))
GRAPH_PEOPLE = os.environ.get("GRAPH_PEOPLE_TSV", "webapp/data/graph_people.tsv.gz")
COMMON_NAME_ZIPS = 5

ROLE_QUERIES = {
    "candidate": "SELECT DISTINCT source_name FROM relationships WHERE source_data = 'FEC_STRUCTURE' "
                 "AND relation_type IN ('CANDIDATE_COMMITTEE', 'LEADERSHIP_PAC')",
    "legislator": "SELECT DISTINCT source_name FROM relationships WHERE source_data IN "
                  "('STATE_LEGISLATURE_WIKI', 'CONGRESS_GITHUB', 'WIKIDATA_POLITICIAN') AND source_type = 'PERSON'",
    "judge": "SELECT DISTINCT source_name FROM relationships WHERE source_data IN ('CL_JUDGES', 'CL_FIN_DISCLOSURE') "
             "AND source_type = 'PERSON'",
    "sec_insider": "SELECT DISTINCT source_name FROM relationships WHERE source_data IN ('SEC_FORM_4', 'RECONCILED_FORM4') "
                   "AND source_type = 'PERSON'",
    "lobbyist": "SELECT DISTINCT source_name FROM relationships WHERE source_data = 'LDA_LOBBYISTS' "
                "AND relation_type = 'LOBBYIST'",
}

_COMBINING = dict.fromkeys(range(0x300, 0x370))


def canon_key(name):
    """Same as build_scored_edges.canon_key."""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = re.sub(r"\s+", " ", re.sub(r"[^\w ]|_", "", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


def common_first_last():
    """first+last canon keys that many different FEC donors share."""
    zips = defaultdict(set)
    for p in glob.glob(os.path.join(HERE, "data", "fec_indiv_*.jsonl.gz")):
        with gzip.open(p, "rt", encoding="utf-8") as f:
            for line in f:
                d = json.loads(line)
                if d.get("ent") == "IND":
                    key, _, z = d.get("dkey", "").partition("|")
                    if key:
                        zips[key].add(z)
    return {k for k, v in zips.items() if len(v) >= COMMON_NAME_ZIPS}


def first_last(key):
    toks = key.split()
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else key


def main():
    with gzip.open(GRAPH_PEOPLE, "rt", encoding="utf-8") as f:
        people = {}
        for line in f:
            k, _, display = line.rstrip("\n").partition("\t")
            if k:
                people[k] = display or k
    common = common_first_last()
    print(f"{len(people):,} graph people; {len(common):,} common donor names")
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '600s'")
    roles = defaultdict(set)
    for role, q in ROLE_QUERIES.items():
        cur.execute(q)
        names = [r[0] for r in cur.fetchall() if r[0]]
        kept = dropped_common = 0
        for n in names:
            k = canon_key(n)
            if k not in people or len(k.split()) < 2 or not re.search(r"[a-z]{2}", k):
                continue
            if first_last(k) in common:
                dropped_common += 1
                continue
            roles[people[k]].add(role)
            kept += 1
        print(f"  {role:12s} {len(names):>7,} names -> {kept:>7,} public ({dropped_common:,} common names left out)")
    conn.close()
    out = {n: sorted(r) for n, r in sorted(roles.items())}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"wrote {OUT}: {len(out):,} people public by role")


if __name__ == "__main__":
    main()
