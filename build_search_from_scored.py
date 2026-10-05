"""Build search_index.json directly from the scored graph nodes.

Simple & safe: every graph node becomes a searchable entry by its own name.
No fuzzy cross-person merging (which previously mis-merged distinct people).
Degree is taken from the scored edge list so high-connectivity hubs rank first.
"""
import gzip, json, os, math
import networkx as nx
from pagerank_util import nx_pagerank_exact
from collections import Counter

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/search_index.json.gz"

print("Loading scored graph...")
with gzip.open(SCORED, "rt", encoding="utf-8") as f:
    ed = json.load(f)
nodes = ed["nodes"]
edges = ed["edges"]

# 1. Build NetworkX graph to calculate PageRank Centrality
print("Building NetworkX graph...")
g = nx.Graph()
g.add_nodes_from(nodes)
deg = Counter()
for u, v, prob, c in edges:
    deg[u] += 1
    deg[v] += 1
    g.add_edge(nodes[u], nodes[v], weight=float(prob))

print("Calculating PageRank Centrality...")
# exact PageRank -- nx.pagerank stopped after one iteration on this graph
# (see pagerank_util.py)
pr = nx_pagerank_exact(g, weight="weight")

# Sort nodes by PageRank ascending to map to 1-100 percentiles
sorted_nodes = sorted(nodes, key=lambda n: pr.get(n, 0.0))
node_percentiles = {}
num_nodes = len(sorted_nodes)
for idx, name in enumerate(sorted_nodes):
    pct = round((idx / num_nodes) * 100)
    node_percentiles[name] = max(1, min(100, pct))

# PageRank ladder for crawlie facts ("#1 is ..., #1,000 is ..."): the person at
# each power-of-ten rank by the SAME PageRank the site's SCI uses. Build-dir
# only (read by build_crawlie_facts.py), not deployed.
# People = nodes the database types PERSON (build_person_nodes.py). The name
# heuristic alone put "SERVICE EMPLOYEES", then "DLA Piper", at #1.
from build_crawlie_facts import looks_like_person
try:
    with gzip.open("webapp/data/person_nodes.json.gz", "rt", encoding="utf-8") as f:
        _person_idx = set(json.load(f))
    is_person = lambda i, n: i in _person_idx
except FileNotFoundError:
    print("WARNING: person_nodes.json.gz missing -- ladder falls back to the name heuristic")
    is_person = lambda i, n: looks_like_person(n)
people_ranked = sorted((n for i, n in enumerate(nodes) if is_person(i, n)), key=lambda n: -pr.get(n, 0.0))
name_to_idx = {n: i for i, n in enumerate(nodes)}
rungs = []
r = 1
while r <= len(people_ranked):
    name = people_ranked[r - 1]
    rungs.append({"rank": r, "name": name, "degree": deg.get(name_to_idx[name], 0)})
    r *= 10
with open("webapp/data/pagerank_ladder.json", "w", encoding="utf-8") as f:
    # "top": the ten highest-PageRank people, exact order -- the "most
    # influential" crawlies use this (they used the 1-100 SCI bucket plus a
    # name-based person guess, which put "WARNOCK FOR GEORGIA" at #1)
    top = [{"rank": k + 1, "name": n, "degree": deg.get(name_to_idx[n], 0)} for k, n in enumerate(people_ranked[:10])]
    json.dump({"people": len(people_ranked), "rungs": rungs, "top": top}, f, ensure_ascii=False, indent=1)
print(f"PageRank ladder: {len(people_ranked):,} people; " + ", ".join(f"#{x['rank']:,} {x['name']}" for x in rungs))

print("Assembling search index...")
index = []
for i, name in enumerate(nodes):
    index.append({
        "canonical": name,
        "normalized": name.lower(),
        "degree": deg.get(i, 0),
        "sci": node_percentiles.get(name, 1),
        "aliases": [],
    })

# Sort by canonical for stable output
index.sort(key=lambda e: e["canonical"].lower())

with gzip.open(OUT, "wt", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

print(f"Wrote {len(index)} search entries to {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB)")
# Sanity: confirm key hubs are present
for name in ["Donald Trump", "Jeffrey Epstein", "Gavin Newsom"]:
    present = any(e["canonical"] == name for e in index)
    d = next((e["degree"] for e in index if e["canonical"] == name), 0)
    sci = next((e["sci"] for e in index if e["canonical"] == name), 1)
    print(f"  {name}: {'OK' if present else 'MISSING'} (degree {d} | SCI {sci})")
