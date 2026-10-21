"""Hierarchical communities with stable ids (2026-10-21, Phase 1).

Phase 0 (scratchpad test, 10-21) showed recursive Leiden gives meaningful
levels: worlds (the establishment, party money, unions, biotech boards...),
then circles (judges, NYC politics, the Koch movement...), then local or
institutional circles. This builds that hierarchy nightly-ready:

  * same affiliation graph as build_graph_stats.community_stats: donation-only
    edges dropped, weight = edge probability, seeded modularity Leiden;
  * level 1 is the whole graph; a community of MIN_SPLIT+ nodes is split
    inside its own subgraph if that partition's modularity is >= MIN_Q;
  * communities of MIN_SIZE+ nodes get a stable id ("C123"), carried over
    from the previous run when the members mostly match (Jaccard >= MATCH),
    so summaries written for a community survive small nightly changes;
  * flags: "cohort" when school ties are over MAX_EDUCATION_SHARE of the
    links inside (a generation, not a circle -- the nightly community facts
    skip these too), "hub_glued" when the two best-connected members carry
    over HUB_SHARE of the links inside;
  * "nameable" people only: verified (verified_people.json.gz: Wikidata
    identity or public by role) AND not a common first+last name (5+ FEC
    donor ZIPs, as build_public_by_role.py, or MAX_NAMESAKES+ person nodes
    in the graph), since a name-level verified list makes every "Matt Smith"
    node look like the famous one.

Writes webapp/data/communities.json.gz (id -> level, parent, size, people,
top orgs, nameable people, glue, flags) and webapp/data/community_members.tsv.gz
(node name -> chain of ids, top level first). No database access.

  python build_community_hierarchy.py               # in graphfinder-clean, like build_graph_stats
  python build_community_hierarchy.py --data DIR    # read and write another webapp/data
"""
import argparse
import collections
import gzip
import json
import os
import random
import re
import sys
import time

import igraph as ig
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_public_by_role import canon_key, common_first_last, first_last  # noqa: E402

MIN_SPLIT = 500
MIN_Q = 0.25
MAX_LEVEL = 4
MIN_SIZE = 50
MATCH = 0.5
MAX_EDUCATION_SHARE = 0.40
HUB_SHARE = 0.5
MAX_NAMESAKES = 4
TOP = 15
T0 = time.time()


def log(msg):
    print(f"{time.time() - T0:7.0f}s {msg}", flush=True)


def load(data):
    with gzip.open(os.path.join(data, "graph_scored.json.gz"), "rt", encoding="utf-8") as f:
        g = json.load(f)
    nodes = g["nodes"]
    person = np.zeros(len(nodes), bool)
    with gzip.open(os.path.join(data, "person_nodes.json.gz"), "rt", encoding="utf-8") as f:
        person[json.load(f)] = True
    with gzip.open(os.path.join(data, "verified_people.json.gz"), "rt", encoding="utf-8") as f:
        verified = set(json.load(f))
    edges = [e for e in g["edges"] if not (len(e) > 3 and set(e[3]) <= {"DONATION"})]
    del g
    A = np.array([e[0] for e in edges], np.int64)
    B = np.array([e[1] for e in edges], np.int64)
    W = np.array([max(float(e[2]), 1e-6) for e in edges])
    cats = [tuple(e[3]) if len(e) > 3 else () for e in edges]
    return nodes, person, verified, A, B, W, cats


def hierarchy(n, A, B, W):
    """labels[L][node] = community at level L+1; parent[L][c] = its community at level L."""
    G = ig.Graph(n=n, edges=np.column_stack([A, B]).tolist(), directed=False)
    G.es["w"] = W.tolist()
    random.seed(42)          # igraph draws from Python's RNG
    labels = [np.array(G.community_leiden(objective_function="modularity", weights="w", n_iterations=4).membership)]
    parent = [{}]
    log(f"level 1: {labels[0].max() + 1:,} communities")
    for level in range(2, MAX_LEVEL + 1):
        prev = labels[-1]
        order = np.argsort(prev, kind="stable")
        nxt, par, nid, n_split = np.empty(n, np.int64), {}, 0, 0
        for grp in np.split(order, np.flatnonzero(np.diff(prev[order])) + 1):
            c, sub = int(prev[grp[0]]), None
            if len(grp) >= MIN_SPLIT:
                S = G.induced_subgraph(grp.tolist())
                random.seed(42)
                mm = S.community_leiden(objective_function="modularity", weights="w", n_iterations=4).membership
                if max(mm) >= 1 and S.modularity(mm, weights="w") >= MIN_Q:
                    sub = np.array(mm)
            if sub is None:
                nxt[grp], par[nid] = nid, c
                nid += 1
            else:
                n_split += 1
                nxt[grp] = nid + sub
                for j in range(sub.max() + 1):
                    par[nid + j] = c
                nid += sub.max() + 1
        labels.append(nxt)
        parent.append(par)
        log(f"level {level}: split {n_split:,} -> {nid:,} communities")
        if not n_split:
            break
    return labels, parent


def describe(nodes, person, nameable, A, B, cats, labels, parent):
    """Per level: {local id: description} for communities of MIN_SIZE+ nodes."""
    n = len(nodes)
    out = []
    parent_size = None
    for li, lab in enumerate(labels):
        la, lb = lab[A], lab[B]
        inside = la == lb
        size = np.bincount(lab)
        big = size >= MIN_SIZE
        if li:   # a community not split further is carried down unchanged: it is not a new one
            big &= np.array([size[c] < parent_size[parent[li][c]] for c in range(len(size))])
        parent_size = size
        e_in = np.bincount(la[inside], minlength=len(size))
        e_bd = np.bincount(la[~inside], minlength=len(size)) + np.bincount(lb[~inside], minlength=len(size))
        ideg = np.bincount(A[inside], minlength=n) + np.bincount(B[inside], minlength=n)
        glue = collections.defaultdict(collections.Counter)
        for i in np.flatnonzero(inside & big[la]):
            for x in cats[i]:
                glue[int(la[i])][x] += 1
        order = np.argsort(lab, kind="stable")
        level = {}
        for grp in np.split(order, np.flatnonzero(np.diff(lab[order])) + 1):
            c = int(lab[grp[0]])
            if not big[c]:
                continue
            top = grp[np.argsort(-ideg[grp], kind="stable")]
            tot = sum(glue[c].values()) or 1
            edu = glue[c]["EDUCATION"] / tot   # as build_graph_stats; with SCHOOL_AFFILIATION the establishment itself counts
            hub = (ideg[top[:2]].sum() / max(1, 2 * e_in[c]))
            flags = (["cohort"] if edu > MAX_EDUCATION_SHARE else []) + (["hub_glued"] if hub > HUB_SHARE else [])
            level[c] = {
                "level": li + 1, "parent_local": parent[li].get(c) if li else None,
                "size": int(size[c]), "people": int(person[grp].sum()),
                "edges_in": int(e_in[c]), "edges_out": int(e_bd[c]),
                "conductance": round(float(e_bd[c] / max(1, 2 * e_in[c] + e_bd[c])), 3),
                "glue": [[k, round(v / tot, 3)] for k, v in glue[c].most_common(6)],
                "flags": flags,
                "top_orgs": [[nodes[i], int(ideg[i])] for i in top if not person[i]][:TOP],
                "nameable_people": [[nodes[i], int(ideg[i])] for i in top if person[i] and nameable[i]][:TOP],
                "_members": grp,
            }
        out.append(level)
        log(f"level {li + 1}: {len(level):,} communities of {MIN_SIZE}+ nodes "
            f"({sum('cohort' in d['flags'] for d in level.values()):,} cohorts, "
            f"{sum('hub_glued' in d['flags'] for d in level.values()):,} hub-glued)")
    return out


def previous(data):
    """Last run's members: node name -> chain of stable ids; and the id counter."""
    chains, next_id = {}, 1
    try:
        with gzip.open(os.path.join(data, "communities.json.gz"), "rt", encoding="utf-8") as f:
            next_id = json.load(f)["next_id"]
        with gzip.open(os.path.join(data, "community_members.tsv.gz"), "rt", encoding="utf-8") as f:
            for line in f:
                name, _, chain = line.rstrip("\n").partition("\t")
                chains[name] = chain.split(",")
    except (FileNotFoundError, KeyError, ValueError):
        pass
    return chains, next_id


def assign_ids(nodes, levels, prev_chains, next_id):
    """Stable ids, matched level by level to last run's communities by member
    overlap (Jaccard >= MATCH, best matches first, each old id used once)."""
    prev_size = collections.Counter(c for ch in prev_chains.values() for c in ch)
    reused = 0
    for li, level in enumerate(levels):
        cands = []
        for c, d in level.items():
            votes = collections.Counter(prev_chains[nodes[i]][li] for i in d["_members"]
                                        if nodes[i] in prev_chains and len(prev_chains[nodes[i]]) > li)
            for old, k in votes.most_common(3):
                j = k / (d["size"] + prev_size[old] - k)
                if j >= MATCH:
                    cands.append((j, c, old))
        taken, used = {}, set()
        for j, c, old in sorted(cands, reverse=True):
            if c not in taken and old not in used:
                taken[c] = old
                used.add(old)
        for c, d in level.items():
            if c in taken:
                d["id"] = taken[c]
                reused += 1
            else:
                d["id"] = f"C{next_id}"
                next_id += 1
    for li, level in enumerate(levels):
        for d in level.values():
            p = d.pop("parent_local")
            d["parent"] = levels[li - 1][p]["id"] if li and p in levels[li - 1] else None
    return next_id, reused


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="webapp/data")
    args = ap.parse_args()
    nodes, person, verified, A, B, W, cats = load(args.data)
    log(f"affiliation graph: {len(nodes):,} nodes, {len(A):,} edges, {person.sum():,} people")
    common = common_first_last()
    # the local FEC extracts are small (Matt Smith isn't at 5 ZIPs), so also
    # count the graph's own person nodes per first+last: 4+ (variants, per-
    # employer splits, namesakes) means the name can't be pinned to one person.
    # Drops some real figures split into variants (Nelson Peltz, 7) -- for
    # summaries, leaving someone out beats naming the wrong one.
    fl = [first_last(canon_key(re.sub(r"\s*\(.*\)\s*$", "", nm))) if p else "" for nm, p in zip(nodes, person)]
    per_name = collections.Counter(k for k in fl if k)
    nameable = np.array([bool(p) and nm.lower() in verified and k not in common and per_name[k] < MAX_NAMESAKES
                         for nm, p, k in zip(nodes, person, fl)])
    log(f"{len(common):,} common donor names; {nameable.sum():,} nameable people "
        f"(of {sum(1 for nm, p in zip(nodes, person) if p and nm.lower() in verified):,} verified)")
    labels, parent = hierarchy(len(nodes), A, B, W)
    levels = describe(nodes, person, nameable, A, B, cats, labels, parent)
    prev_chains, next_id = previous(args.data)
    next_id, reused = assign_ids(nodes, levels, prev_chains, next_id)
    total = sum(len(lv) for lv in levels)
    log(f"{total:,} communities; {reused:,} kept last run's id, {total - reused:,} new")

    chains = {}
    for level in levels:
        for d in level.values():
            for i in d.pop("_members"):
                chains.setdefault(i, []).append(d["id"])
    comms = {d["id"]: d for level in levels for d in level.values()}
    tmp = os.path.join(args.data, "communities.json.gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "next_id": next_id,
                   "params": {"MIN_SPLIT": MIN_SPLIT, "MIN_Q": MIN_Q, "MAX_LEVEL": MAX_LEVEL, "MIN_SIZE": MIN_SIZE,
                              "MATCH": MATCH}, "communities": comms}, f, ensure_ascii=False)
    os.replace(tmp, os.path.join(args.data, "communities.json.gz"))
    tmp = os.path.join(args.data, "community_members.tsv.gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        for i in sorted(chains):
            f.write(f"{nodes[i]}\t{','.join(chains[i])}\n")
    os.replace(tmp, os.path.join(args.data, "community_members.tsv.gz"))
    log(f"wrote communities.json.gz ({len(comms):,}) and community_members.tsv.gz ({len(chains):,} nodes)")


if __name__ == "__main__":
    main()
