"""Investment-adviser owners into the graph, labelled by firm (2026-10-16).

The SEC_ADV harvest (Form ADV Schedule A/B: owners and control persons of
SEC-registered investment advisers) writes relation_type OWNERSHIP, which
build_scored_edges drops entirely, so advisers were missing from the graph.
This copies the usable rows to a separate set, source_data SEC_ADV_OWNERS,
relation ADVISER_OWNER (categorize() -> CO_EXECUTIVE). The SEC_ADV rows are
left as they are.

  * Junk "people" from the ADV parser are dropped: company fragments ("LLC
    HOLDINGS"), places ("South Korea", "Grand D Luxembourg"), form labels
    ("CCO CEO") and firm names read as people ("LYNCH MERRILL").
  * The stray middle "I" the parser inserts ("DONALD I YI") is removed.
  * Each person is one node named "Name (Firm)" after the adviser they appear
    with most, so an owner can't merge with a namesake elsewhere in the graph
    ("John Kerry" owns an adviser; the senator doesn't). ADV gives no CRD for
    individuals, so the same name at several advisers is taken as one person.

  python load_sec_adv_owners.py                       # dry run
  python load_sec_adv_owners.py --commit              # insert
  python load_sec_adv_owners.py --commit --replace    # delete SEC_ADV_OWNERS first (same transaction)
Undo: DELETE FROM relationships WHERE source_data = 'SEC_ADV_OWNERS';
"""
import argparse
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_crawlie_facts import is_clean_label, looks_like_person  # noqa: E402
from load_ai_financing import short_firm  # noqa: E402

SOURCE = "SEC_ADV_OWNERS"
ORG_TOKENS = {
    "llc", "inc", "lp", "ltd", "gp", "llp", "corp", "co", "plc", "sa", "ag", "nv", "bv", "gmbh", "trust", "trustee",
    "holdings", "holding", "holdco", "partners", "partnership", "partner", "management", "capital", "group",
    "advisors", "advisers", "advisory", "company", "companies", "fund", "funds", "investments", "investment",
    "securities", "financial", "bank", "member", "committee", "director", "officer", "counsel", "limited",
    "sec", "ceo", "cco", "cfo", "coo", "cio", "annual", "initial", "amendment", "delaware", "under", "estate",
    "foundation", "family", "office", "the", "of", "and", "national", "pricewaterhousecoopers"}
PLACES = {
    "afghanistan", "argentina", "australia", "austria", "bahamas", "bahrain", "belgium", "bermuda", "brazil",
    "british", "canada", "cayman", "chile", "china", "colombia", "cyprus", "denmark", "dubai", "duchy", "emirates",
    "england", "finland", "france", "germany", "gibraltar", "greece", "grand", "guernsey", "hong", "kong", "india",
    "indonesia", "ireland", "islands", "isle", "israel", "italy", "japan", "jersey", "korea", "kingdom", "kuwait",
    "liechtenstein", "luxembourg", "malaysia", "malta", "mauritius", "mexico", "monaco", "netherlands", "zealand",
    "norway", "panama", "peru", "philippines", "poland", "portugal", "qatar", "republic", "russia", "saudi",
    "scotland", "singapore", "spain", "sweden", "switzerland", "taiwan", "thailand", "turkey", "uae", "ukraine",
    "uruguay", "virgin", "wales", "alberta", "ontario", "quebec", "columbia", "toronto", "vancouver", "montreal",
    "vaud", "geneva", "zurich", "nyon", "lugano", "london", "paris", "sydney", "melbourne", "victoria", "tokyo",
    "seoul", "beijing", "shanghai", "mumbai", "states", "united", "people's"}
# firms that own advisers and come through as (often reversed) "people"
FIRM_NAMES = {frozenset(f.split()) for f in (
    "merrill lynch", "pierce merrill lynch", "frank rimerman", "clayton dubilier", "stifel nicolaus",
    "goldman sachs", "morgan stanley", "charles schwab", "raymond james", "edward jones")}


def tokens(s):
    return s.lower().replace(",", " ").replace(".", " ").replace("&", " ").split()


def clean_name(n):
    toks = n.split()
    if len(toks) >= 3:
        toks = [toks[0]] + [t for t in toks[1:-1] if t != "I"] + [toks[-1]]   # 'DONALD I YI' -> 'DONALD YI'
    n = " ".join(toks)
    if n.isupper():
        n = n.title()
        n = re.sub(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), n)
    return n


def build_rows(cur):
    cur.execute("SELECT DISTINCT target_name FROM relationships WHERE source_data = 'SEC_ADV'")
    firm_sets = defaultdict(list)
    for (f,) in cur.fetchall():
        ts = frozenset(tokens(f))
        for t in ts:
            firm_sets[t].append(ts)
    cur.execute("SELECT source_name, target_name, evidence FROM relationships "
                "WHERE source_data = 'SEC_ADV' AND relation_type = 'OWNERSHIP' AND source_type = 'PERSON'")
    raw = cur.fetchall()

    def junk(name):
        toks = tokens(name)
        if len(toks) < 2 or any(t in ORG_TOKENS or t in PLACES for t in toks):
            return True
        if frozenset(toks) in FIRM_NAMES:
            return True
        ts = set(toks)
        return any(ts <= fs for fs in firm_sets.get(toks[0], ()))       # 'LYNCH MERRILL'

    kept, dropped = [], Counter()
    for src, firm, ev in raw:
        name = clean_name(src)
        if junk(src) or not is_clean_label(name) or not looks_like_person(name):
            dropped[src] += 1
            continue
        try:
            crd = (json.loads(ev) if isinstance(ev, str) else ev or {}).get("crd")
        except ValueError:
            crd = None
        kept.append((name, firm, crd, src))

    firms_of = defaultdict(Counter)
    for name, firm, _, _ in kept:
        firms_of[name][firm] += 1
    label = {n: f"{n} ({short_firm(min(c.items(), key=lambda kv: (-kv[1], kv[0]))[0])})" for n, c in firms_of.items()}

    rows, seen = [], set()
    for name, firm, crd, src in kept:
        key = (label[name], firm)
        if key in seen:
            continue
        seen.add(key)
        rows.append((None, label[name], "PERSON", f"CRD:{crd}" if crd else None, firm, "ORG", "ADVISER_OWNER", SOURCE,
                     json.dumps({"source": SOURCE, "crd": crd, "advisor_name": firm, "adv_name": src,
                                 "from": "SEC_ADV Form ADV owners/control persons"})))
    return rows, raw, dropped, firms_of


def main():
    ap = argparse.ArgumentParser(allow_abbrev=False)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--replace", action="store_true", help=f"delete {SOURCE} rows first, same transaction")
    args = ap.parse_args()
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import psycopg2
    import pg_secret
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    rows, raw, dropped, firms_of = build_rows(cur)
    people = {r[1] for r in rows}
    firms = {r[4] for r in rows}
    print(f"SEC_ADV owner rows {len(raw):,} -> {len(rows):,} {SOURCE} rows: {len(people):,} people at {len(firms):,} advisers")
    print(f"dropped {sum(dropped.values()):,} rows ({len(dropped):,} names), e.g. "
          + ", ".join(n for n, _ in dropped.most_common(12)))
    random.seed(0)
    print("sample labels:", ", ".join(random.sample(sorted(people), 12)))
    print("most advisers:", ", ".join(f"{n} {sum(c.values())}" for n, c in
                                      sorted(firms_of.items(), key=lambda kv: -sum(kv[1].values()))[:12]))
    if not args.commit:
        print("dry run -- add --commit to insert")
        return
    if args.replace:
        cur.execute("DELETE FROM relationships WHERE source_data = %s", (SOURCE,))
        print(f"deleted {cur.rowcount:,} old {SOURCE} rows")
    inserted = execute_values(
        cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, "
             "relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING RETURNING 1",
        rows, page_size=5000, fetch=True)
    conn.commit()
    print(f"inserted {len(inserted):,} of {len(rows):,} rows (the rest already existed)")


if __name__ == "__main__":
    main()
