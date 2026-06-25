"""Build search_index.json directly from the scored graph nodes.

Simple & safe: every graph node becomes a searchable entry by its own name.
No fuzzy cross-person merging (which previously mis-merged distinct people).
Degree is taken from the scored edge list so high-connectivity hubs rank first.
"""
import gzip, json, os, math
import networkx as nx
from collections import Counter

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/search_index.json"

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
pr = nx.pagerank(g, weight="weight", max_iter=200)

# Sort nodes by PageRank ascending to map to 1-100 percentiles
sorted_nodes = sorted(nodes, key=lambda n: pr.get(n, 0.0))
node_percentiles = {}
num_nodes = len(sorted_nodes)
for idx, name in enumerate(sorted_nodes):
    pct = round((idx / num_nodes) * 100)
    node_percentiles[name] = max(1, min(100, pct))

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

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

print(f"Wrote {len(index)} search entries to {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB)")
# Sanity: confirm key hubs are present
for name in ["Donald Trump", "Jeffrey Epstein", "Gavin Newsom"]:
    present = any(e["canonical"] == name for e in index)
    d = next((e["degree"] for e in index if e["canonical"] == name), 0)
    sci = next((e["sci"] for e in index if e["canonical"] == name), 1)
    print(f"  {name}: {'OK' if present else 'MISSING'} (degree {d} | SCI {sci})")
