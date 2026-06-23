"""Cheap batched VERIFIED resolution: one SPARQL query resolves many names AND
pulls their key facts (employer/party/position/school/spouse labels) inline.
Disambiguate by which QID's facts overlap the person's existing graph neighbors.

~1 query per batch (20-40 names) instead of ~20 calls per person.
"""
import requests, json, gzip, re, time
from collections import defaultdict

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def batch_resolve(names):
    """For a batch of names, return {name: [(qid, {fact_labels})...]} candidates
    with their human-ness and key related-entity labels, in ONE query."""
    values = " ".join(f'"{n}"@en' for n in names)
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
    for attempt in range(3):
        try:
            r = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                             headers=HEADERS, timeout=120)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1)); continue
            rows = r.json()["results"]["bindings"]
            break
        except Exception:
            time.sleep(2 * (attempt + 1))
    else:
        return {}
    # group: name -> qid -> set(fact labels)
    out = defaultdict(lambda: defaultdict(set))
    for row in rows:
        nm = row.get("name", {}).get("value")
        qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        fact = row.get("factLabel", {}).get("value", "")
        if nm and qid:
            if fact and not fact.startswith("Q"):
                out[nm][qid].add(norm(fact))
            else:
                out[nm][qid]  # ensure qid present even with no facts
    return out


def resolve_batch_verified(names, graph_nbrs):
    """Pick the best QID per name by neighbor-fact overlap."""
    cands = batch_resolve(names)
    results = {}
    for name in names:
        nbrs = graph_nbrs.get(name.lower(), set())
        best = None
        for qid, facts in cands.get(name, {}).items():
            score = len(nbrs & facts)
            if best is None or score > best[1]:
                best = (qid, score, nbrs & facts)
        results[name] = best
    return results


if __name__ == "__main__":
    with gzip.open("webapp/data/graph_scored.json.gz", "rt", encoding="utf-8") as f:
        ed = json.load(f)
    n2i = {n.lower(): i for i, n in enumerate(ed["nodes"])}
    adj = defaultdict(list)
    for u, v, p, c in ed["edges"]:
        adj[u].append(v); adj[v].append(u)
    nodes = ed["nodes"]

    test = ["Robert Rubin", "Henry Paulson", "Steven Mnuchin", "Hillary Clinton",
            "Jeffrey Epstein", "Bill Clinton", "Gary Cohn", "Larry Summers"]
    gnbrs = {}
    for nm in test:
        i = n2i.get(nm.lower())
        gnbrs[nm.lower()] = {norm(nodes[j]) for j in adj.get(i, ())} if i is not None else set()

    t0 = time.time()
    res = resolve_batch_verified(test, gnbrs)
    dt = time.time() - t0
    print(f"Resolved {len(test)} names in ONE batch query ({dt:.1f}s)\n")
    for nm, best in res.items():
        if best and best[1] > 0:
            print(f"  {nm:16} -> {best[0]} CONFIRMED ({best[1]} shared: {list(best[2])[:3]})")
        elif best:
            print(f"  {nm:16} -> {best[0]} unverified (0 shared)")
        else:
            print(f"  {nm:16} -> UNRESOLVED")
