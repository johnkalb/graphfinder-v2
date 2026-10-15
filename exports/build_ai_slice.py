"""Gephi slice of the AI / neocloud financing web (2026-10-15).

Two layers in one graph:
  * deal edges from ai_deals.py (hand-compiled, sourced): who pays / lends /
    invests / leases to whom, summed per direction pair
  * people from the sixdegrees graph: directors, officers, executives and SEC
    insiders of each company in the deal web (all of a company's spelling
    variants merged), keeping everyone tied to 2+ companies (interlocks) and
    the strongest-linked others up to PEOPLE_PER_COMPANY each

  python build_ai_slice.py   -> ai_cloud_slice.gexf + ai_deals.csv (next to this file)
"""
import csv
import gzip
import json
import math
import os
import re
from collections import Counter, defaultdict

import networkx as nx

from ai_deals import DEALS, GRAPH_PATTERNS, KIND, RING

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = r"C:\Users\johnk\graphfinder-clean\webapp\data"
PEOPLE_PER_COMPANY = 12
GOV = {"CO_DIRECTOR", "CO_OFFICER", "CO_EXECUTIVE", "SEC_INSIDER"}
NOT_THE_COMPANY = re.compile(r"\b(PAC|POLITICAL ACTION|FOUNDATION|EMPLOYEES?|BENEFIT|PLAN|TRUST|FUND)\b", re.I)
CONF_RANK = {"company": 3, "press": 2, "reported": 1}


def main():
    g = json.load(gzip.open(os.path.join(DATA, "graph_scored.json.gz"), "rt", encoding="utf-8"))
    nodes, edges = g["nodes"], g["edges"]
    with gzip.open(os.path.join(DATA, "graph_people.tsv.gz"), "rt", encoding="utf-8") as f:
        people = {line.rstrip("\n").split("\t", 1)[1] for line in f if "\t" in line}

    # company -> its graph node indices (every spelling variant)
    variants = defaultdict(list)
    for company, pat in GRAPH_PATTERNS.items():
        rx = re.compile(pat, re.I)
        for i, n in enumerate(nodes):
            if rx.search(n) and not NOT_THE_COMPANY.search(n):
                variants[company].append(i)
    owner = {i: c for c, ids in variants.items() for i in ids}

    # governance people around each company
    links = defaultdict(dict)            # person idx -> {company: (prob, categories)}
    for a, b, p, cat in edges:
        cats = set(cat) if isinstance(cat, list) else {cat}
        if not cats & GOV:
            continue
        for co_i, other in ((a, b), (b, a)):
            c = owner.get(co_i)
            if c and nodes[other] in people:
                prev = links[other].get(c)
                if prev is None or p > prev[0]:
                    links[other][c] = (p, "+".join(sorted(cats)))
    # SEC insider filings write "Musk Elon" / "Porat Ruth": fold name variants
    # that share the same first+last tokens (ignoring order and initials)
    def name_key(n):
        toks = [t for t in re.sub(r"[^\w ]", " ", n.lower()).split() if len(t) > 1 and t not in ("jr", "sr", "ii", "iii")]
        return frozenset(toks[:1] + toks[-1:]) if len(toks) >= 2 else n.lower()
    by_key = defaultdict(list)
    for i in links:
        by_key[name_key(nodes[i])].append(i)
    for ids in by_key.values():
        if len(ids) > 1:
            main_i = max(ids, key=lambda i: (len(links[i]), not nodes[i].isupper(), len(nodes[i])))
            for i in ids:
                if i != main_i:
                    for c, v in links.pop(i).items():
                        if c not in links[main_i] or v[0] > links[main_i][c][0]:
                            links[main_i][c] = v
    keep = {i for i, cs in links.items() if len(cs) >= 2}          # interlocks
    by_company = defaultdict(list)
    for i, cs in links.items():
        for c, (p, _) in cs.items():
            by_company[c].append((p, i))
    for c, lst in by_company.items():
        for p, i in sorted(lst, reverse=True)[:PEOPLE_PER_COMPANY]:
            keep.add(i)

    G = nx.DiGraph()
    entities = {d[1] for d in DEALS} | {d[2] for d in DEALS}
    inflow, outflow = Counter(), Counter()
    for d in DEALS:
        if d[4]:
            outflow[d[1]] += d[4]
            inflow[d[2]] += d[4]
    for e in sorted(entities):
        kind = KIND.get(e, "investor")
        G.add_node(e, label=e, kind=kind, ring=RING[kind], in_bn=float(round(inflow[e], 2)), out_bn=float(round(outflow[e], 2)),
                   size=float(round(4 + 3 * math.log10(1 + inflow[e] + outflow[e]), 2)),
                   graph_names="; ".join(nodes[i] for i in variants.get(e, []))[:500])

    pair = defaultdict(list)
    for d in DEALS:
        pair[(d[1], d[2])].append(d)
    for (a, b), ds in pair.items():
        total = sum(d[4] or 0 for d in ds)
        G.add_edge(a, b, layer="deal", relation="+".join(sorted({d[3] for d in ds})),
                   value_bn=float(round(total, 2)), deals=len(ds),
                   weight=float(round(1 + math.log10(1 + total), 3)),
                   confidence=max((d[6] for d in ds), key=CONF_RANK.get),
                   dates=", ".join(sorted({d[0] for d in ds})),
                   detail=" | ".join(f"{d[0]} {d[3]} {'$%sB' % d[4] if d[4] else 'undisclosed'}: {d[5]}" for d in ds)[:1000],
                   sources=" ".join(sorted({d[7] for d in ds}))[:1000])

    for i in keep:
        name = nodes[i]
        cs = links[i]
        # a common name tied to 3+ big companies is more likely several people
        # merged by the graph than one person -- flag it, don't drop it
        G.add_node(name, label=name, kind="person", ring=5, in_bn=0.0, out_bn=0.0,
                   size=float(round(2 + 1.5 * len(cs), 2)), companies=len(cs), graph_names=name,
                   check_name="yes" if len(cs) >= 3 and name.split()[0] not in ("Elon", "Antonio") else "")
        for c, (p, cats) in cs.items():
            G.add_edge(name, c, layer="people", relation=cats, value_bn=0.0, deals=0, weight=float(round(p, 3)),
                       confidence="sixdegrees", dates="", detail="", sources="sixdegrees graph")

    out = os.path.join(HERE, "ai_cloud_slice.gexf")
    nx.write_gexf(G, out)
    with open(os.path.join(HERE, "ai_deals.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "from", "to", "type", "value_bn", "note", "confidence", "source"])
        w.writerows(DEALS)

    kinds = Counter(G.nodes[n]["kind"] for n in G)
    print(f"{G.number_of_nodes()} nodes {dict(kinds)}, {G.number_of_edges()} edges -> {out}")
    print("matched graph variants:", {c: len(v) for c, v in sorted(variants.items())})
    print("no graph match:", sorted(e for e in entities if e not in variants))
    inter = sorted(((len(links[i]), nodes[i], sorted(links[i])) for i in keep if len(links[i]) >= 2), reverse=True)
    print(f"interlocking people ({len(inter)}):")
    for n, name, cs in inter[:40]:
        print(f"   {name}: {', '.join(cs)}")
    print("biggest in/out ($bn):", [(e, round(inflow[e]), round(outflow[e])) for e in
                                    sorted(entities, key=lambda e: -(inflow[e] + outflow[e]))[:12]])


if __name__ == "__main__":
    main()
