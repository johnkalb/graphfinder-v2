"""Production VERIFIED QID resolver for sixdegrees graph people.

Resolves person-like graph nodes to Wikidata QIDs using batched SPARQL +
shared-fact verification (a candidate QID is accepted only if its
employer/party/position/school/spouse facts overlap the person's existing
graph neighbors). This rejects namesakes (e.g. the Holocaust-victim "Robert
Rubin" vs the Treasury Secretary).

Sharded for optional Tailscale-fleet parallelism:
  python qid_resolver.py --shards 5 --shard 0   # node 0 of 5
  python qid_resolver.py                         # whole thing, single machine

Output: qid_map.shardN.jsonl (one {name, qid, score, facts} per resolved person)
Merge shards with: cat qid_map.shard*.jsonl > qid_map.jsonl

Only writes CONFIRMED resolutions (>=1 shared fact) — unverified names are
left UNRESOLVED rather than risk a wrong-namesake QID.
"""
import requests, json, gzip, re, time, argparse
from collections import defaultdict, Counter

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}
BATCH = 30
MIN_DEGREE = 20
SLEEP = 1.0


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
                time.sleep(4 * (attempt + 1)); continue
            rows = r.json()["results"]["bindings"]
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        return {}
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards", type=int, default=1)
    ap.add_argument("--shard", type=int, default=0)
    args = ap.parse_args()

    with gzip.open("webapp/data/graph_scored.json.gz", "rt", encoding="utf-8") as f:
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

    targets = [i for i in range(len(nodes)) if person_like(nodes[i]) and deg[i] >= MIN_DEGREE]
    # deterministic shard split
    targets = [t for k, t in enumerate(targets) if k % args.shards == args.shard]
    print(f"shard {args.shard}/{args.shards}: {len(targets)} targets", flush=True)

    out_path = f"qid_map.shard{args.shard}.jsonl"
    done_names = set()
    try:
        with open(out_path) as f:
            for line in f:
                done_names.add(json.loads(line)["name"].lower())
    except FileNotFoundError:
        pass

    out_f = open(out_path, "a", encoding="utf-8")
    confirmed = 0
    for bstart in range(0, len(targets), BATCH):
        chunk = targets[bstart:bstart + BATCH]
        names = [nodes[i] for i in chunk if nodes[i].lower() not in done_names]
        if not names:
            continue
        gnbrs = {nm.lower(): {norm(nodes[j]) for j in adj.get(n2i[nm.lower()], ())}
                 for nm in names}
        cands = batch_resolve(names)
        for nm in names:
            nbrs = gnbrs.get(nm.lower(), set())
            best = None
            for qid, facts in cands.get(nm, {}).items():
                score = len(nbrs & facts)
                if best is None or score > best[1]:
                    best = (qid, score, list(nbrs & facts)[:5])
            if best and best[1] > 0:
                out_f.write(json.dumps({"name": nm, "qid": best[0],
                                        "score": best[1], "facts": best[2]}) + "\n")
                confirmed += 1
        out_f.flush()
        time.sleep(SLEEP)
        if (bstart // BATCH) % 20 == 0:
            print(f"  [{bstart+len(chunk)}/{len(targets)}] confirmed={confirmed}", flush=True)

    out_f.close()
    print(f"DONE shard {args.shard}: {confirmed} confirmed QIDs -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
