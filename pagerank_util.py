"""Exact weighted PageRank for a networkx graph, computed with igraph (2026-10-04).

nx.pagerank(g, max_iter=200) stops when the total change is below
len(g) * tol (tol=1e-6). On the 1.8M-node graph that threshold is 1.8 -- more
than all the probability mass -- so it returned after ONE iteration. That
score mostly rewarded being linked to low-degree nodes: the site's SCI agreed
with a one-step PageRank on 92% of nodes and with the converged one on 4%,
and the "who you know" crawlies kept finding people with 15 contacts who
"outranked" Gavin Newsom, whose converged rank is #737 of 1.56M people.
igraph's PRPACK solver is exact, and takes seconds instead of minutes.
"""
import igraph as ig


def nx_pagerank_exact(g, weight="weight", damping=0.85):
    """{node: PageRank} for every node of networkx graph g, isolated ones included."""
    names = list(g.nodes())
    ix = {n: i for i, n in enumerate(names)}
    edges, weights = [], []
    for a, b, d in g.edges(data=True):
        edges.append((ix[a], ix[b]))
        weights.append(float(d.get(weight, 1.0)))
    G = ig.Graph(n=len(names), edges=edges, directed=g.is_directed())
    return dict(zip(names, G.pagerank(weights=weights, damping=damping)))
