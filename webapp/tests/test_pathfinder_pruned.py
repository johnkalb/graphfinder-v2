"""_k_shortest_pruned (2026-10-18) must give the same path weights as igraph's
full get_k_shortest_paths, on small random graphs with hubs and inf edges."""
import math
import random

import igraph as ig
import numpy as np
import pytest

import pathfinder as pf


def _weights(g, vp, w):
    return round(sum(w[g.get_eid(vp[i], vp[i + 1])] for i in range(len(vp) - 1)), 9)


@pytest.mark.parametrize("seed", range(6))
def test_pruned_matches_full_yen(monkeypatch, seed):
    rnd = random.Random(seed)
    random.seed(seed)  # igraph's generators draw from the random module
    g = ig.Graph.Barabasi(3000, 2) + ig.Graph.Tree(30, 2)  # second piece: a separate component
    g.simplify()
    w = np.array([1.0 + rnd.random() * 3 for _ in range(g.ecount())])
    living = w.copy()
    living[rnd.sample(range(g.ecount()), 300)] = math.inf
    for attr, val in {"_igraph_graph": g, "_igraph_weight": w, "_igraph_living_weight": living}.items():
        monkeypatch.setattr(pf, attr, val)
    for attr in ("_igraph_csr", "_igraph_csr_living", "_igraph_csr_eid", "_igraph_csr_for", "_igraph_component"):
        monkeypatch.setattr(pf, attr, None)
    pf._build_igraph_csr()
    assert pf._igraph_csr_for is g
    for _ in range(25):
        s, t = rnd.sample(range(g.vcount()), 2)
        for weights in (w, living):
            got = pf._k_shortest_pruned(s, t, 3, weights)
            want = [vp for vp in g.get_k_shortest_paths(s, t, k=3, weights=weights, output="vpath")
                    if len(vp) > 1 and math.isfinite(_weights(g, vp, weights))]
            if got is None:  # fell back to the full search: allowed, just not for this tiny graph
                pytest.fail(f"no certified answer for {s}->{t}")
            assert [_weights(g, vp, weights) for vp in got] == [_weights(g, vp, weights) for vp in want]
