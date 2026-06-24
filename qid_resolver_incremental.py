"""Incremental background QID resolver for sixdegrees.net.

Gradually resolves graph people (degree >= MIN_DEGREE) to verified Wikidata QIDs,
a small polite batch per run, so a daily cron can grind through the whole
population over weeks without hitting rate limits.

Reuses the proven verification approach (batched SPARQL + shared-fact cross-check
against existing graph neighbors) so namesakes are rejected. For every NEWLY
confirmed person it also pulls P570 (date of death) so the deceased list grows
alongside the QID map.

State files (in $HOME):
  qid_map.jsonl        - confirmed {name, qid, score, facts}   (appended)
  qid_attempted.txt    - every name we've TRIED (one per line)  (so we never
                         re-query the ~92% that don't resolve)
  deceased.json        - {lowercase name: death date} (post-1900 only)

Per run it processes up to BUDGET people not yet attempted, highest-degree first.
Prints a short report ONLY when it confirms new people or finishes; otherwise
stays quiet (watchdog pattern) so the daily cron doesn't spam.

Run: cd ~ && PYTHONPATH="$HOME" ./venv/Scripts/python qid_resolver_incremental.py
"""
import os, sys, json, re, time
import requests
from collections import defaultdict, Counter

HOME = os.path.expanduser("~")
GRAPH = os.path.join(HOME, "webapp", "data", "graph_scored.json.gz")
QID_MAP = os.path.join(HOME, "qid_map.jsonl")
ATTEMPTED = os.path.join(HOME, "qid_attempted.txt")
DECEASED = os.path.join(HOME, "webapp", "data", "deceased.json")

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}
BATCH = 30
MIN_DEGREE = 3        # full coverage target (highest-degree first)
BUDGET = 600          # people attempted per run (~20 batches, a few minutes)
SLEEP = 1.2


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def batch_resolve(names):
    values = " ".join(f'"{n}"@en' for n in names if '"' not in n)
    if not values.strip():
        return {}
    query = f"""
SELECT ?name ?person ?factLabel WHERE {{
  VALUES ?name {{ {values} }}
  ?person rdfs:label ?name .
  ?person wdt:P31 wd:Q5 .
  OPTIONAL {{
    ?person ?p ?fact .
    VALUES ?p {{ wdt:P108 wdt:P102 wdt:P39 wdt:P69 wdt:P26 wdt:P463 wdt:P1416 }}
  }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
    for attempt in range(4):
        try:
            r = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                             headers=HEADERS, timeout=120)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1)); continue
            rows = r.json()["results"]["bindings"]
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        return None  # signal hard failure (don't mark attempted)
    out = defaultdict(lambda: defaultdict(set))
    for row in rows:
        nm = row.get("name", {}).get("value")
        qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        fact = row.get("factLabel", {}).get("value", "")
        if nm and qid:
            if fact and not re.match(r"^Q\d+$", fact):
                out[nm][qid].add(norm(fact))
            else:
                _ = out[nm][qid]
    return out


def fetch_all_death_dates(qids):
    """Return {qid: 'YYYY-MM-DD'} for ALL with P570 (any era), for QA filtering."""
    if not qids:
        return {}
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"SELECT ?person ?death WHERE {{ VALUES ?person {{ {values} }} ?person wdt:P570 ?death . }}"
    for attempt in range(3):
        try:
            r = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                             headers=HEADERS, timeout=90)
            if r.status_code == 429:
                time.sleep(4 * (attempt + 1)); continue
            rows = r.json()["results"]["bindings"]
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        return {}
    out = {}
    for row in rows:
        qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        death = row.get("death", {}).get("value", "")[:10]
        if qid and death:
            out[qid] = death
    return out


def fetch_death_dates(qids):
    """Post-1900 deaths only (for the live deceased list)."""
    return {q: d for q, d in fetch_all_death_dates(qids).items()
            if d[:4].isdigit() and int(d[:4]) >= 1900}


def main():
    with __import__("gzip").open(GRAPH, "rt", encoding="utf-8") as f:
        ed = json.load(f)
    nodes = ed["nodes"]
    n2i = {n.lower(): i for i, n in enumerate(nodes)}
    adj = defaultdict(list)
    deg = Counter()
    for u, v, p, c in ed["edges"]:
        adj[u].append(v); adj[v].append(u); deg[u] += 1; deg[v] += 1

    ORG = re.compile(r"\b(Inc|Corp|Company|Group|Foundation|University|College|Bank|"
                     r"Capital|Institute|Committee|Council|Trust|Fund|LLC|Ltd|Holdings|"
                     r"Partners|Associates|School|Systems|Corporation|PAC|Victory|For|"
                     r"Campaign|Securities|Management)\b", re.I)
    def person_like(nm):
        if ORG.search(nm) or any(ch.isdigit() for ch in nm):
            return False
        return 2 <= len(nm.split()) <= 4

    # targets, highest-degree first
    targets = [i for i in range(len(nodes)) if person_like(nodes[i]) and deg[i] >= MIN_DEGREE]
    targets.sort(key=lambda i: -deg[i])

    # already attempted (confirmed or tried-and-failed)
    attempted = set()
    if os.path.exists(ATTEMPTED):
        with open(ATTEMPTED, encoding="utf-8") as f:
            attempted = {ln.strip().lower() for ln in f if ln.strip()}
    if os.path.exists(QID_MAP):
        with open(QID_MAP, encoding="utf-8") as f:
            for ln in f:
                try:
                    attempted.add(json.loads(ln)["name"].lower())
                except Exception:
                    pass

    todo = [nodes[i] for i in targets if nodes[i].lower() not in attempted]
    total_remaining = len(todo)
    todo = todo[:BUDGET]
    if not todo:
        # nothing left — fully resolved. Silent unless you want a final ping.
        return

    qid_to_name = {}  # for the death-date pass
    confirmed = 0
    rejected_historical = 0
    att_f = open(ATTEMPTED, "a", encoding="utf-8")
    newly = []
    pending = []  # (name, qid, score, facts) awaiting death-date QA before commit
    for bstart in range(0, len(todo), BATCH):
        names = todo[bstart:bstart + BATCH]
        gnbrs = {nm.lower(): {norm(nodes[j]) for j in adj.get(n2i[nm.lower()], ())}
                 for nm in names}
        cands = batch_resolve(names)
        if cands is None:
            break  # hard failure; stop, don't mark these attempted (retry next run)
        for nm in names:
            att_f.write(nm + "\n")  # mark attempted regardless of outcome
            nbrs = gnbrs.get(nm.lower(), set())
            best = None
            for qid, facts in cands.get(nm, {}).items():
                score = len(nbrs & facts)
                if best is None or score > best[1]:
                    best = (qid, score, list(nbrs & facts)[:5])
            if best and best[1] > 0:
                pending.append((nm, best[0], best[1], best[2]))
        att_f.flush()
        time.sleep(SLEEP)
    att_f.close()

    # Death-date QA: reject candidates whose QID died pre-1900 (wrong historical
    # namesake, e.g. "Adam Smith" -> the 1790 economist). Record post-1900 deaths.
    all_deaths = {}
    if pending:
        all_deaths = fetch_all_death_dates([q for _, q, _, _ in pending])
    map_f = open(QID_MAP, "a", encoding="utf-8")
    new_deaths = 0
    deceased_existing = {}
    if os.path.exists(DECEASED):
        try:
            with open(DECEASED, encoding="utf-8") as f:
                deceased_existing = {k.lower(): v for k, v in json.load(f).items()}
        except Exception:
            deceased_existing = {}
    for nm, qid, score, facts in pending:
        death = all_deaths.get(qid)
        if death and death[:4].isdigit() and int(death[:4]) < 1900:
            rejected_historical += 1
            continue  # bad namesake match — do NOT add to qid_map
        map_f.write(json.dumps({"name": nm, "qid": qid,
                                "score": score, "facts": facts}) + "\n")
        qid_to_name[qid] = nm
        newly.append(nm)
        confirmed += 1
        if death and death[:4].isdigit() and int(death[:4]) >= 1900:
            if nm.lower() not in deceased_existing:
                deceased_existing[nm.lower()] = death
                new_deaths += 1
    map_f.close()
    if new_deaths:
        with open(DECEASED, "w", encoding="utf-8") as f:
            json.dump(deceased_existing, f, indent=0, sort_keys=True)

    # Report only when something happened (watchdog pattern)
    if confirmed or new_deaths or rejected_historical:
        remaining_after = total_remaining - len(todo)
        print(f"QID resolver: +{confirmed} verified identities this run "
              f"({len(todo)} attempted, ~{max(0,remaining_after)} still to go).")
        if new_deaths:
            print(f"  +{new_deaths} death dates recorded.")
        if rejected_historical:
            print(f"  {rejected_historical} rejected as wrong historical-namesake matches (pre-1900 death).")
        print("Note: qid_map.jsonl grew; run the overlap engine + refresh to use new identities in paths.")


if __name__ == "__main__":
    main()
