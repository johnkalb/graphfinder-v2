"""Evaluate the staged Congress ties before they go live (spec section 9).

Adds the staged pairs (data/congress_ties_candidate.pkl.gz, from
`congress_ties.py export`) to a copy of today's scored graph exactly as
build_scored_edges.py would (noisy-OR with existing links), then:

  1. known pairs: well-documented congressional working relationships --
     does each get a direct Congress link?
  2. path diff: 100 member->member and 100 member->outsider best paths,
     before vs after (same -log p + forwarding costs as the site; deceased
     people excluded as the site does by default).

  python congress_eval.py      # writes data/congress_eval_report.txt
"""
import collections
import gzip
import json
import math
import os
import pickle
import random
import sys

import igraph as ig

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "webapp"))
from link_scoring import FORWARD_PROB  # noqa: E402

GRAPH = os.path.join(HERE, "webapp", "data", "graph_scored.json.gz")
TIES = os.path.join(HERE, "data", "congress_ties_candidate.pkl.gz")
DECEASED = os.path.join(HERE, "webapp", "data", "deceased.json")
OUT = os.path.join(HERE, "data", "congress_eval_report.txt")

# Public, well-documented working relationships (committee chair/ranking
# pairs, leadership counterparts, bill partners, same-state senators).
KNOWN = [
    ("Barney Frank", "Spencer Bachus"), ("Henry Waxman", "Joe Barton"), ("John Boehner", "Nancy Pelosi"),
    ("Edward Kennedy", "Orrin Hatch"), ("John McCain", "Russ Feingold"), ("Patrick Leahy", "Orrin Hatch"),
    ("Chuck Grassley", "Max Baucus"), ("Dianne Feinstein", "Barbara Boxer"), ("Tom Daschle", "Trent Lott"),
    ("Harry Reid", "Mitch McConnell"), ("Lindsey Graham", "John McCain"), ("Ron Wyden", "Jeff Merkley"),
    ("Paul Ryan", "Patty Murray"), ("Chris Dodd", "Richard Shelby"), ("Joe Lieberman", "John McCain"),
    ("James Inhofe", "Barbara Boxer"), ("Mike Enzi", "Edward Kennedy"), ("John Dingell", "Henry Waxman"),
    ("Bob Corker", "Ben Cardin"), ("Mark Warner", "Richard Burr"), ("Adam Schiff", "Devin Nunes"),
    ("Chuck Schumer", "Kirsten Gillibrand"), ("Elizabeth Warren", "Ed Markey"), ("Bernie Sanders", "Patrick Leahy"),
    ("Mitch McConnell", "Rand Paul"), ("Kevin McCarthy", "Steve Scalise"), ("Nancy Pelosi", "Steny Hoyer"),
    ("Dick Durbin", "Tammy Duckworth"), ("Tom Harkin", "Chuck Grassley"), ("Pete Domenici", "Jeff Bingaman"),
]


def label_ix(nodes):
    ix = collections.defaultdict(list)
    for i, n in enumerate(nodes):
        ix[n.lower()].append(i)
    return ix


def build_graph(nodes, edges, extra, dead):
    """extra: {(i, j): (p, cat)} combined by noisy-OR with any existing edge.
    Deceased people's links are left out (the site's default: no deceased
    go-betweens; query endpoints are never deceased here)."""
    pe = {}
    for s, t, p, cats in edges:
        if s in dead or t in dead:
            continue
        pe[(s, t) if s < t else (t, s)] = [p, cats]
    n_new = n_comb = 0
    for k, (p, cat) in extra.items():
        if k[0] in dead or k[1] in dead:
            continue
        if k in pe:
            pe[k] = [1 - (1 - pe[k][0]) * (1 - p), pe[k][1] + [cat]]
            n_comb += 1
        else:
            pe[k] = [p, [cat]]
            n_new += 1
    keys = list(pe)
    g = ig.Graph(n=len(nodes), edges=keys, directed=False)
    fwd = -math.log(FORWARD_PROB)
    g.es["w"] = [-math.log(max(min(pe[k][0], 1 - 1e-9), 1e-9)) + fwd for k in keys]
    g.es["p"] = [pe[k][0] for k in keys]
    g.es["c"] = [pe[k][1] for k in keys]
    return g, n_new, n_comb


def best_path(g, s, t, dead):
    if s == t:
        return None
    vp = g.get_shortest_paths(s, to=t, weights="w", output="vpath")[0]
    if len(vp) < 2:
        return None
    eids = g.get_eids(list(zip(vp[:-1], vp[1:])))
    prob = math.prod(g.es[e]["p"] for e in eids) * FORWARD_PROB ** (len(vp) - 2)
    return vp, eids, prob


def main():
    g0 = json.load(gzip.open(GRAPH, "rt", encoding="utf-8"))
    nodes = list(g0["nodes"])
    st = pickle.load(gzip.open(TIES, "rb"))
    ties, label = st["ties"], st["label"]
    ix = label_ix(nodes)
    for lab in set(label.values()):
        if lab.lower() not in ix:
            ix[lab.lower()].append(len(nodes))
            nodes.append(lab)
    node_of = {b: ix[lab.lower()][0] for b, lab in label.items()}
    extra = {}
    for a, b, p, strength, expl in ties:
        i, j = node_of[a], node_of[b]
        if i != j:
            extra[(i, j) if i < j else (j, i)] = (p, "CONGRESS_COLLEAGUE" if strength == "strong" else "CONGRESS_WEAK_TIE")
    print(f"graph {len(g0['nodes']):,} nodes (+{len(nodes) - len(g0['nodes'])} new members), "
          f"{len(g0['edges']):,} edges; Congress pairs {len(extra):,}", flush=True)
    dead_names = set(json.load(open(DECEASED, encoding="utf-8")))
    dead = {i for i, n in enumerate(nodes) if n.lower() in dead_names}
    before, _, _ = build_graph(nodes, g0["edges"], {}, dead)
    after, n_new, n_comb = build_graph(nodes, g0["edges"], extra, dead)
    lines = [f"Congress ties evaluation -- {len(extra):,} pairs: {n_new:,} new links, {n_comb:,} combined with existing",
             ""]

    # 1. known pairs
    name_ix = {n.lower(): node_of[b] for b, n in ((b, l) for b, l in label.items())}
    import congress_ties as ct
    members = ct.load_model()["members"]
    by_name = {}
    for b, lab in label.items():
        by_name.setdefault(lab.lower().split(" (")[0], node_of[b])
        for v in ct.name_variants(members[b]):
            by_name.setdefault(v, node_of[b])
    tie_of = {}
    for a, b, p, strength, expl in ties:
        tie_of[frozenset((node_of[a], node_of[b]))] = (p, strength, expl)
    lines.append("1. Known working relationships: direct Congress link?")
    hit = 0
    for x, y in KNOWN:
        i, j = by_name.get(x.lower()), by_name.get(y.lower())
        if i is None or j is None:
            lines.append(f"   ?    {x} / {y}: not found under these names")
            continue
        t = tie_of.get(frozenset((i, j)))
        if t:
            hit += 1
            lines.append(f"   yes  {x} / {y}: {t[1]} p={t[0]:.2f} -- {t[2][:140]}")
        else:
            lines.append(f"   no   {x} / {y}")
    lines.insert(len(lines) - len(KNOWN), "")
    lines.append(f"   => {hit} of {len(KNOWN)} have a direct Congress link")
    lines.append("")

    # 2. path diff
    rng = random.Random(11)
    members = sorted({node_of[b] for b in label if node_of[b] < len(g0["nodes"]) and node_of[b] not in dead})
    deg = before.degree()
    outsiders = [i for i in sorted(range(len(g0["nodes"])), key=lambda i: -deg[i])[:20000]
                 if i not in set(members) and i not in dead and " " in nodes[i] and "(" not in nodes[i]]
    queries = [("member-member", *rng.sample(members, 2)) for _ in range(100)] + \
              [("member-outsider", rng.choice(members), rng.choice(outsiders[:5000])) for _ in range(100)]
    changed, stats = [], collections.Counter()
    for kind, s, t in queries:
        b0, b1 = best_path(before, s, t, dead), best_path(after, s, t, dead)
        if not b0 or not b1:
            stats[f"{kind}: no path"] += 1
            continue
        stats[f"{kind}: total"] += 1
        if b0[0] != b1[0]:
            stats[f"{kind}: changed"] += 1
            uses = any(set(after.es[e]["c"]) & {"CONGRESS_COLLEAGUE", "CONGRESS_WEAK_TIE"} for e in b1[1])
            stats[f"{kind}: changed via a Congress link"] += uses
            changed.append((kind, b0, b1))
        stats[f"{kind}: hops before"] += len(b0[0]) - 1
        stats[f"{kind}: hops after"] += len(b1[0]) - 1
        stats[f"{kind}: log prob gain"] += math.log(b1[2]) - math.log(b0[2])
    lines.append("2. Best-path comparison (same costs as the site)")
    for kind in ("member-member", "member-outsider"):
        n = stats[f"{kind}: total"] or 1
        lines.append(f"   {kind}: {stats[f'{kind}: total']} queries, {stats[f'{kind}: changed']} changed "
                     f"({stats[f'{kind}: changed via a Congress link']} via a Congress link); "
                     f"mean hops {stats[f'{kind}: hops before'] / n:.2f} -> {stats[f'{kind}: hops after'] / n:.2f}; "
                     f"mean path probability x{math.exp(stats[f'{kind}: log prob gain'] / n):.2f}")
    lines.append("")
    lines.append("3. Changed paths to judge (before / after, probability)")

    def fmt(g, b):
        vp, eids, p = b
        parts = [nodes[vp[0]]]
        for e, v in zip(eids, vp[1:]):
            parts.append(f"-[{'/'.join(sorted(set(g.es[e]['c'])))}]-> {nodes[v]}")
        return f"{p:.4f}  " + " ".join(parts)

    for kind, b0, b1 in changed[:40]:
        lines.append(f"   [{kind}] {nodes[b0[0][0]]} -> {nodes[b0[0][-1]]}")
        lines.append(f"      before {fmt(before, b0)}")
        lines.append(f"      after  {fmt(after, b1)}")
    open(OUT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("\n".join(lines[:60]))
    print(f"... full report: {OUT}")


if __name__ == "__main__":
    main()
