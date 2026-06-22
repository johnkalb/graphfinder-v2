"""Build search_index.json directly from the scored graph nodes.

Simple & safe: every graph node becomes a searchable entry by its own name.
No fuzzy cross-person merging (which previously mis-merged distinct people).
Degree is taken from the scored edge list so high-connectivity hubs rank first.
"""
import gzip, json, os
from collections import Counter

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/search_index.json"

with gzip.open(SCORED, "rt", encoding="utf-8") as f:
    ed = json.load(f)
nodes = ed["nodes"]
edges = ed["edges"]

# Degree per node id
deg = Counter()
for u, v, p, c in edges:
    deg[u] += 1
    deg[v] += 1

index = []
for i, name in enumerate(nodes):
    index.append({
        "canonical": name,
        "normalized": name.lower(),
        "degree": deg.get(i, 0),
        "aliases": [],
    })

# Sort by canonical for stable output
index.sort(key=lambda e: e["canonical"].lower())

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

print(f"Wrote {len(index)} search entries to {OUT} ({os.path.getsize(OUT)/1024/1024:.1f} MB)")
# Sanity: confirm key hubs are present
for name in ["Donald Trump", "President Donald Trump", "Jeffrey Epstein", "Gavin Newsom"]:
    present = any(e["canonical"] == name for e in index)
    d = next((e["degree"] for e in index if e["canonical"] == name), 0)
    print(f"  {name}: {'OK' if present else 'MISSING'} (degree {d})")
