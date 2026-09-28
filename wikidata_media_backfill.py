#!/usr/bin/env python3
"""Wikidata backfill: award-winning actors, directors, producers, writers,
authors, and US journalists (2026-09-28).

What it adds (source_data='WIKIDATA_MEDIA', fully reversible:
    DELETE FROM relationships WHERE source_data='WIKIDATA_MEDIA';):

  * PERSON -AWARD_RECEIVED-> award category   (e.g. "Academy Award for Best Actor")
    for winners of 10 award families (Oscars, Primetime Emmys, Tonys, Grammys,
    Golden Globes, Pulitzers, Peabodys, National Book Award, Booker, Nobel Lit).
  * PERSON -CAST_IN / DIRECTED / PRODUCED / WROTE_SCREENPLAY / AUTHORED-> work,
    only for works shared by >= 2 of those winners (a work with one winner is
    a dead end that adds nodes but no connectivity). Works are named
    "<title> (<year> <kind>)" so they can't collide with people or orgs.
  * PERSON -EMPLOYEE-> employer for US journalists with a listed employer.

Scoring: CAST_IN..AUTHORED -> CREATIVE_COLLAB, AWARD_RECEIVED -> AWARD
(webapp/relation_categories.py + link_scoring.py); EMPLOYEE -> EMPLOYMENT.

Identity -- the graph is keyed by (canonicalized) name, so a Wikidata person
whose name already exists in the graph is only merged into that node when
confirmed: their Wikidata id is in qid_map.jsonl, or at least one of their
Wikidata facts (employer, party, office, school, spouse, membership, family)
matches a graph neighbour of that node -- the same test qid_resolver_incremental
uses. Unconfirmed namesakes get their own node, "<name> (<Wikidata description>)",
so an Oscar-winning Robert Johnson is never welded onto a congressman.

Usage (needs DATABASE_URL for --commit; fetches are cached in CACHE_DIR):
    python wikidata_media_backfill.py            # fetch + resolve + dry-run report
    python wikidata_media_backfill.py --commit   # also write rows to Postgres
"""
import gzip
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict

import requests
try:
    import pip_system_certs.wrapt_requests  # noqa: F401 -- Norton TLS interception on this machine
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get("WD_MEDIA_CACHE", r"C:\Users\johnk\graphfinder-clean\data\wd_media")
GRAPH = os.path.join(HERE, "webapp", "data", "graph_scored.json.gz")
QID_MAP = os.path.join(os.path.expanduser("~"), "qid_map.jsonl")
SOURCE = "WIKIDATA_MEDIA"

EP = "https://query.wikidata.org/sparql"
HDRS = {"User-Agent": "SixDegreesGraph/1.0 (media backfill; sixdegrees.net)",
        "Accept": "application/sparql-results+json"}

AWARD_FAMILIES = {"Academy Awards": "Q19020", "Primetime Emmy": "Q1044427", "Tony": "Q191874",
                  "Grammy": "Q41254", "Golden Globe": "Q1011547", "Pulitzer": "Q46525",
                  "Peabody": "Q838121", "National Book Award": "Q572316", "Booker": "Q160082",
                  "Nobel Literature": "Q37922"}
CREDIT_ROLES = {"P161": "CAST_IN", "P57": "DIRECTED", "P162": "PRODUCED",
                "P58": "WROTE_SCREENPLAY", "P50": "AUTHORED"}
# P50 also covers millions of scientific papers -- restrict to literary works.
LITERARY = " ; wdt:P31/wdt:P279? ?t . VALUES ?t { wd:Q7725634 wd:Q571 wd:Q47461344 wd:Q8261 wd:Q49848 }"
IDENTITY_PROPS = "wdt:P108 wdt:P102 wdt:P39 wdt:P69 wdt:P26 wdt:P463 wdt:P1416 wdt:P451 wdt:P40 wdt:P22 wdt:P25 wdt:P3373"
WORK_KINDS = {"Q11424": "film", "Q506240": "TV film", "Q5398426": "TV series", "Q1259759": "miniseries",
              "Q93204": "documentary", "Q24856": "film series", "Q25379": "play", "Q2743": "musical",
              "Q7725634": "book", "Q571": "book", "Q8261": "novel", "Q47461344": "book", "Q49848": "book"}


# --- helpers -------------------------------------------------------------------

_COMBINING = dict.fromkeys(range(0x300, 0x370))
_CK_KEEP = re.compile(r"[^\w ]|_")
_CK_WS = re.compile(r"\s+")


def canon_key(name):
    """Identical to build_scored_edges.canon_key -- the graph's node-merge key."""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = _CK_WS.sub(" ", _CK_KEEP.sub("", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def sparql(q, tries=4):
    for i in range(tries):
        try:
            r = requests.post(EP, data={"query": q}, headers=HDRS, timeout=120)
        except requests.RequestException:
            time.sleep(10 * (i + 1))
            continue
        if r.status_code == 200:
            time.sleep(1.0)
            return r.json()["results"]["bindings"]
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(int(r.headers.get("Retry-After", 10 * (i + 1))))
            continue
        r.raise_for_status()
    raise RuntimeError("SPARQL failed repeatedly")


def qid(binding, var):
    return binding[var]["value"].rsplit("/", 1)[1]


def val(binding, var):
    return binding.get(var, {}).get("value", "")


def cached(name, fetch):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, name + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    log(f"fetching {name} ...")
    data = fetch()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def batched(items, n):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def values(qids):
    return " ".join(f"wd:{q}" for q in qids)


# --- fetches (each cached) -------------------------------------------------------

def fetch_awards():
    """[(person, award_category_qid, award_label)] for every winner in the 10 families."""
    def children(qs):
        rows = sparql(f"SELECT DISTINCT ?a WHERE {{ VALUES ?f {{ {values(qs)} }} ?a wdt:P31|wdt:P279|wdt:P361 ?f . }}")
        return {qid(r, "a") for r in rows}
    out = []
    for fam, fq in AWARD_FAMILIES.items():
        cats = {fq} | children([fq])
        cats |= children(sorted(cats))
        for b in batched(sorted(cats), 40):
            for r in sparql(f"SELECT ?p ?a ?aLabel WHERE {{ VALUES ?a {{ {values(b)} }} ?p wdt:P166 ?a ; wdt:P31 wd:Q5 . "
                            f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,mul\". }} }}"):
                out.append((qid(r, "p"), qid(r, "a"), val(r, "aLabel")))
        log(f"  awards: {fam} done, {len(out):,} rows so far")
    return out


def fetch_credits(people):
    out = []
    for n, b in enumerate(batched(sorted(people), 50)):
        for prop, rel in CREDIT_ROLES.items():
            extra = LITERARY if prop == "P50" else ""
            for r in sparql(f"SELECT DISTINCT ?p ?w WHERE {{ VALUES ?p {{ {values(b)} }} ?w wdt:{prop} ?p{extra} . }}"):
                out.append((qid(r, "p"), qid(r, "w"), rel))
        if n % 20 == 0:
            log(f"  credits: {min((n + 1) * 50, len(people)):,}/{len(people):,} people, {len(out):,} credits")
    return out


def fetch_journalists():
    rows = sparql("""SELECT ?p ?emp ?empLabel WHERE {
      ?p wdt:P106 wd:Q1930187 ; wdt:P27 wd:Q30 ; wdt:P108 ?emp .
      SERVICE wikibase:label { bd:serviceParam wikibase:language "en,mul". } }""")
    return [(qid(r, "p"), qid(r, "emp"), val(r, "empLabel")) for r in rows]


def fetch_labels(qids, with_desc=False):
    """{qid: [label, description]} (English)."""
    out = {}
    sel = "?x ?xLabel ?xDescription" if with_desc else "?x ?xLabel"
    for n, b in enumerate(batched(sorted(qids), 300)):
        for r in sparql(f"SELECT {sel} WHERE {{ VALUES ?x {{ {values(b)} }} "
                        f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,mul\". }} }}"):
            out[qid(r, "x")] = [val(r, "xLabel"), val(r, "xDescription")]
        if n % 20 == 0:
            log(f"  labels: {min((n + 1) * 300, len(qids)):,}/{len(qids):,}")
    return out


def fetch_work_meta(works):
    """{work: [kind, year]} -- kind from P31, year = earliest P577."""
    out = {}
    for n, b in enumerate(batched(sorted(works), 200)):
        for r in sparql(f"SELECT ?w (SAMPLE(?t) AS ?type) (MIN(YEAR(?d)) AS ?year) WHERE {{ VALUES ?w {{ {values(b)} }} "
                        f"OPTIONAL {{ ?w wdt:P31 ?t . }} OPTIONAL {{ ?w wdt:P577 ?d . }} }} GROUP BY ?w"):
            t = val(r, "type").rsplit("/", 1)[-1]
            out[qid(r, "w")] = [WORK_KINDS.get(t, "work"), val(r, "year")]
        if n % 20 == 0:
            log(f"  work meta: {min((n + 1) * 200, len(works)):,}/{len(works):,}")
    return out


def fetch_identity_facts(qids):
    """{qid: [canonical fact labels]} for the identity check."""
    out = defaultdict(set)
    for n, b in enumerate(batched(sorted(qids), 50)):
        for r in sparql(f"SELECT ?p ?factLabel WHERE {{ VALUES ?p {{ {values(b)} }} ?p ?prop ?fact . "
                        f"VALUES ?prop {{ {IDENTITY_PROPS} }} "
                        f"SERVICE wikibase:label {{ bd:serviceParam wikibase:language \"en,mul\". }} }}"):
            label = val(r, "factLabel")
            if label and not re.fullmatch(r"Q\d+", label):
                out[qid(r, "p")].add(canon_key(label))
        if n % 20 == 0:
            log(f"  identity facts: {min((n + 1) * 50, len(qids)):,}/{len(qids):,}")
    return {k: sorted(v) for k, v in out.items()}


def repair_labels(name, data, with_desc=False):
    """Wikidata has been moving language-neutral names to a "mul" label and
    dropping the English copy, so an English-only lookup returns the bare QID
    (Meryl Streep came back as "Q873", 2026-09-28). Re-read those accepting
    en or mul labels and fix the cache in place."""
    bad = [q for q, v in data.items() if re.fullmatch(r"Q\d+", v[0] or "")]
    if not bad:
        return data
    log(f"  repairing {len(bad):,} bare-QID labels in {name}")
    for b in batched(bad, 100):
        desc = ' OPTIONAL { ?x schema:description ?d . FILTER(LANG(?d) = "en") }' if with_desc else ""
        for r in sparql(f'SELECT ?x ?l ?d WHERE {{ VALUES ?x {{ {values(b)} }} '
                        f'?x rdfs:label ?l . FILTER(LANG(?l) IN ("en", "mul")){desc} }}'):
            data[qid(r, "x")] = [val(r, "l"), val(r, "d") or data[qid(r, "x")][1]]
    with open(os.path.join(CACHE_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def repair_triples(name, data):
    """Same repair for [subject, object_qid, object_label] caches (awards, employers)."""
    bad = sorted({o for _, o, lab in data if re.fullmatch(r"Q\d+", lab or "")})
    if not bad:
        return data
    log(f"  repairing {len(bad):,} bare-QID labels in {name}")
    fixed = {}
    for b in batched(bad, 100):
        for r in sparql(f'SELECT ?x ?l WHERE {{ VALUES ?x {{ {values(b)} }} ?x rdfs:label ?l . FILTER(LANG(?l) IN ("en", "mul")) }}'):
            fixed[qid(r, "x")] = val(r, "l")
    data = [[s, o, fixed.get(o, lab)] for s, o, lab in data]
    with open(os.path.join(CACHE_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def fetch_label_counts(names):
    """{name: number of Wikidata humans whose English label is exactly name}."""
    out = {}
    for b in batched(sorted(names), 50):
        # match the name as an English OR language-neutral ("mul") label
        vals = " ".join(f"{json.dumps(n)}@en {json.dumps(n)}@mul" for n in b)
        for r in sparql(f"SELECT ?s (COUNT(DISTINCT ?p) AS ?n) WHERE {{ VALUES ?name {{ {vals} }} "
                        f"?p rdfs:label ?name ; wdt:P31 wd:Q5 . BIND(STR(?name) AS ?s) }} GROUP BY ?s"):
            out[val(r, "s")] = int(val(r, "n"))
    return out


# Unconfirmed name collisions are still merged when the name is unique among
# Wikidata humans AND the existing node is small: a big node carrying a unique
# Wikidata name is usually an FEC/lobbying aggregate of many namesakes
# ("Chet Atkins", degree 543), while Martin Scorsese / Bob Woodward sit at 16 / 9.
UNIQUE_NAME_MAX_DEGREE = 150


def resolve_people(people, labels, own_targets=None):
    """Map Wikidata people to the graph node name to write, per the module
    docstring's identity rules. Returns (node_name {qid: name}, graph node
    list, canon_key -> node index). Reused by wikidata_influencer_backfill.py
    (its own CACHE_DIR keeps the identity caches separate).

    own_targets: {qid: set of canon_key target names this import will link
    the person to}. A node that already neighbours any of them is the same
    person -- which also makes re-runs idempotent: once an import's rows are
    in the graph, everyone matches their own earlier links instead of being
    split into "(description)" duplicates of themselves (found 2026-09-28)."""
    own_targets = own_targets or {}
    with gzip.open(GRAPH, "rt", encoding="utf-8") as f:
        g = json.load(f)
    gnodes = g["nodes"]
    key_to_idx = {canon_key(n): i for i, n in enumerate(gnodes)}
    collide = {p for p in people if labels.get(p) and canon_key(labels[p][0]) in key_to_idx
               and not re.fullmatch(r"Q\d+", labels[p][0])}
    log(f"people={len(people):,} | name already in graph={len(collide):,}")
    want = {key_to_idx[canon_key(labels[p][0])] for p in collide}
    nbrs = defaultdict(set)
    degree = Counter()
    for e in g["edges"]:
        a, b = e[0], e[1]
        if a in want:
            nbrs[a].add(canon_key(gnodes[b]))
            degree[a] += 1
        if b in want:
            nbrs[b].add(canon_key(gnodes[a]))
            degree[b] += 1
    del g
    facts = cached("identity_facts", lambda: fetch_identity_facts(collide))
    label_counts = cached("label_counts", lambda: fetch_label_counts({labels[p][0] for p in collide}))
    qmap = {}
    if os.path.exists(QID_MAP):
        for line in open(QID_MAP, encoding="utf-8"):
            if line.strip():
                rec = json.loads(line)
                qmap[rec["qid"]] = rec["name"]

    node_name = {}          # person qid -> graph node name to write
    how = Counter()
    for p in people:
        label, desc = (labels.get(p) or ["", ""])
        if not label or re.fullmatch(r"Q\d+", label):
            how["no English label (skipped)"] += 1
            continue
        if p not in collide:
            node_name[p] = label
            how["new person"] += 1
            continue
        idx = key_to_idx[canon_key(label)]
        if (canon_key(qmap.get(p, "")) == canon_key(label) or set(facts.get(p, [])) & nbrs[idx]
                or own_targets.get(p, set()) & nbrs[idx]):
            node_name[p] = gnodes[idx]
            how["merged into existing node (confirmed)"] += 1
        elif label_counts.get(label) == 1 and degree[idx] <= UNIQUE_NAME_MAX_DEGREE:
            node_name[p] = gnodes[idx]
            how["merged into existing node (unique name, small node)"] += 1
        elif desc:
            node_name[p] = f"{label} ({desc})"
            how["namesake -> disambiguated node"] += 1
        else:
            how["namesake without description (skipped)"] += 1
    for k, v in how.most_common():
        log(f"  identity: {k}: {v:,}")
    return node_name, gnodes, key_to_idx


# --- resolve + build rows ------------------------------------------------------------

def main():
    commit = "--commit" in sys.argv

    awards = repair_triples("awards", cached("awards", fetch_awards))
    winners = sorted({p for p, _, _ in awards})
    credits = cached("credits", lambda: fetch_credits(winners))
    journalists = repair_triples("journalists", cached("journalists", fetch_journalists))
    log(f"awards={len(awards):,} winners={len(winners):,} credits={len(credits):,} journalist links={len(journalists):,}")

    # works shared by >= 2 winners
    work_people = defaultdict(set)
    for p, w, _ in credits:
        work_people[w].add(p)
    shared = {w for w, ps in work_people.items() if len(ps) >= 2}
    credits = [(p, w, rel) for p, w, rel in credits if w in shared]
    log(f"shared works={len(shared):,} -> credits kept={len(credits):,}")

    people = set(winners) | {p for p, _, _ in journalists}
    labels = repair_labels("person_labels", cached("person_labels", lambda: fetch_labels(people, with_desc=True)),
                           with_desc=True)
    work_labels = repair_labels("work_labels", cached("work_labels", lambda: fetch_labels(shared)))
    work_meta = cached("work_meta", lambda: fetch_work_meta(shared))

    work_name = {}
    for w in shared:
        title = (work_labels.get(w) or [""])[0]
        if not title or re.fullmatch(r"Q\d+", title):
            continue
        kind, year = (work_meta.get(w) or ["work", ""])
        work_name[w] = f"{title} ({year + ' ' if year else ''}{kind})"

    own_targets = defaultdict(set)   # identity evidence: what this import links each person to
    for p, _, alabel in awards:
        own_targets[p].add(canon_key(alabel))
    for p, w, _ in credits:
        if w in work_name:
            own_targets[p].add(canon_key(work_name[w]))
    for p, _, elabel in journalists:
        own_targets[p].add(canon_key(elabel))
    node_name, gnodes, key_to_idx = resolve_people(people, labels, own_targets)

    # --- rows ---
    rows = set()
    for p, a, alabel in awards:
        if p in node_name and alabel and not re.fullmatch(r"Q\d+", alabel):
            rows.add((node_name[p], "PERSON", alabel, "ORG", "AWARD_RECEIVED",
                      json.dumps({"qid": p, "award_qid": a, "src": "wdqs P166"})))
    for p, w, rel in credits:
        if p in node_name and w in work_name:
            rows.add((node_name[p], "PERSON", work_name[w], "ORG", rel,
                      json.dumps({"qid": p, "work_qid": w, "src": "wdqs credits"})))
    for p, emp, elabel in journalists:
        if p in node_name and elabel and not re.fullmatch(r"Q\d+", elabel):
            target = gnodes[key_to_idx[canon_key(elabel)]] if canon_key(elabel) in key_to_idx else elabel
            rows.add((node_name[p], "PERSON", target, "ORG", "EMPLOYEE",
                      json.dumps({"qid": p, "employer_qid": emp, "src": "wdqs P108 journalists"})))
    # one row per (source, target, relation) -- the table's unique key
    uniq = {}
    for r in rows:
        uniq.setdefault((r[0], r[2], r[4]), r)
    rows = list(uniq.values())
    by_rel = Counter(r[4] for r in rows)
    log(f"rows to write: {len(rows):,}  " + ", ".join(f"{k}={v:,}" for k, v in by_rel.most_common()))
    log(f"distinct people: {len({r[0] for r in rows}):,} | distinct targets: {len({r[2] for r in rows}):,}")
    for sample_person in ("Meryl Streep", "Tom Hanks", "Steven Spielberg", "Martin Scorsese", "Bob Woodward", "Toni Morrison"):
        mine = [r for r in rows if r[0] == sample_person][:6]
        log(f"  sample {sample_person}: " + "; ".join(f"{r[4]} -> {r[2]}" for r in mine))
    with open(os.path.join(CACHE_DIR, "rows_preview.jsonl"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    if not commit:
        log("dry run -- pass --commit to write")
        return
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   [(None, s, st, None, t, tt, rel, SOURCE, ev) for s, st, t, tt, rel, ev in rows], page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")
    conn.close()


if __name__ == "__main__":
    main()
