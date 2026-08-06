import gzip
import json
import math
import networkx as nx

SCORED = "webapp/data/graph_scored.json.gz"

with gzip.open(SCORED, "rt", encoding="utf-8") as f:
    ed = json.load(f)
nodes = ed["nodes"]
edges = ed["edges"]

# Build NetworkX graph
g = nx.Graph()
g.add_nodes_from(nodes)
for u, v, prob, cats in edges:
    # Use prob as weight for PageRank (stronger connection = more flow)
    g.add_edge(nodes[u], nodes[v], weight=float(prob))

print("Calculating PageRank (this may take 5-15s)...")
pr = nx.pagerank(g, weight="weight")

# Sort nodes by PageRank ascending to map to percentiles
sorted_nodes = sorted(nodes, key=lambda n: pr.get(n, 0.0))

print("Top 15 hubs by PageRank Centrality:")
# The last items in sorted_nodes have the highest PageRank
for idx in range(len(sorted_nodes) - 1, len(sorted_nodes) - 16, -1):
    node = sorted_nodes[idx]
    val = pr[node]
    percentile = round((idx / len(nodes)) * 100)
    # Ensure percentile is at least 1 and at most 100
    percentile = max(1, min(100, percentile))
    print(f"  {len(sorted_nodes) - idx}. {node} (PageRank: {val:.6f} | Percentile SCI: {percentile})")
