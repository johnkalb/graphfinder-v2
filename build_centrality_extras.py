"""Compute non-PageRank centrality measures for the crawlie ticker: top people
by raw degree, and "bridge" nodes (high betweenness centrality) with a label
for what they connect.

Runs after build_scored_edges.py, on the same graph_scored.json.gz. Kept as
its own script (not folded into build_search_from_scored.py) so a slow/failed
run here never blocks the PageRank pass or the rest of the pipeline -- see
rebuild_and_deploy.py's chain, where this step is allowed to fail without
aborting the run (unlike the other build_*.py steps).

Betweenness centrality: exact computation is infeasible at this graph's size
(847K nodes, 3.9M edges -- Brandes' algorithm is O(V*E), i.e. ~10^12
operations from every source). Uses networkx's k-sampled approximation
instead (a random subset of source nodes, not all of them), UNWEIGHTED
(ignores the noisy-OR probability on each edge entirely -- weighted
betweenness wants edge weight to mean "distance", but this graph's weight
means "confidence", and Dijkstra-per-source at this edge count was
empirically too slow for any usable sample size; unweighted BFS-per-source
is ~10s/source and k=100 fits a ~15-20min budget). This is a real, documented
approximation, not a bug -- both scores and exact top-N ranks will vary
slightly between runs. Same "surprising rank"-style tolerance the rest of
this feature already accepts.

"What a bridge connects": Louvain community detection (near-linear time,
~5min on the full graph -- unlike betweenness this doesn't need sampling)
partitions the graph once. For each top-betweenness person, look at which
communities their neighbors fall into and report the two largest, each
labeled by ITS highest-degree member (a real, recognizable name) since
communities are just numeric IDs with no inherent name of their own.

Output: webapp/data/centrality_extras.json.gz
  {"top_degree": [{"name","degree"}, ...],
   "bridges": [{"name","betweenness","community_a","community_b"}, ...]}
"""
import gzip
import json
import os
import re
import time

import networkx as nx
from networkx.algorithms.community import louvain_communities

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/centrality_extras.json.gz"

TOP_DEGREE_COUNT = 10
TOP_BRIDGE_COUNT = 3
BETWEENNESS_SAMPLE_K = 100
BETWEENNESS_SEED = 42
MIN_DEGREE = 15  # same floor build_crawlie_facts.py uses -- skip near-isolated stub nodes

# Same org-word heuristic as build_crawlie_facts.py's looks_like_person() --
# don't invent a second one. A "bridge" or "top by degree" candidate that's
# really an org/PAC/LLC isn't the compelling "who connects what" story this
# category is for (confirmed empirically: raw unweighted betweenness's
# unfiltered top-5 today were 4 orgs/committees and 1 person).
_ORG_WORDS = re.compile(
    r"\b(Inc|Corp|LLC|Company|Co|Group|Foundation|University|College|Bank|Partners|"
    r"Holdings|Trust|Fund|Capital|Ltd|Institute|Center|Centre|Committee|Council|"
    r"Systems|Technologies|Services|Corporation|Enterprises|Industries|Management|"
    r"Ventures|Associates|Media|Properties|Realty|Organization|Association|Society|"
    r"School|Hospital|Church|Authority|Commission|Bureau|Agency|Department|"
    r"National|International|Global|Network|Union|League|Academy|Museum|Library|"
    r"Times|Post|Journal|News|Press|Labs|Laboratory|Office|Board)\b",
    re.I,
)


def looks_like_person(name):
    if _ORG_WORDS.search(name):
        return False
    if any(ch.isdigit() for ch in name):
        return False
    return 2 <= len(name.split()) <= 3


# Confirmed real problem 2026-09-03: "Michael Smith" surfaced as the #1
# betweenness bridge candidate AND as two different communities' label (degree
# 1296, spanning 8 wildly unrelated source_data types -- a patent co-inventor
# at a slot-machine company, an IRS-990 officer at two different accounting
# firms, an SEC director at a chemical company, three different Wikipedia
# alma maters -- clearly dozens of distinct real "Michael Smith"s merged into
# one node by name-only entity resolution, not one prolific person). Tried a
# general category-diversity heuristic first (a collision artifact should
# span implausibly many relation categories) -- it didn't discriminate
# cleanly: Michael Smith's 13 categories was LESS than Barack Obama's 15 or
# Donald Trump's 19, since a genuinely famous multi-faceted person
# legitimately accumulates just as much category spread as a collision does.
# Tried a full-name blocklist next (just "Michael Smith" and similar
# combinations) -- confirmed insufficient: re-running with it excluded, the
# SAME giant community (110,640 members) surfaced a DIFFERENT common name
# ("William Harris") as its next-highest-degree member instead. This is
# clearly a whole class of collisions in that one cluster, not a handful of
# specific names -- blocklisting full names one at a time would mean an
# expensive ~22min rerun to discover each new offender, indefinitely.
# Generalized instead: flag a name as a likely collision if BOTH its first
# and last token are independently among the most common in the US (top ~40
# each, common-knowledge US Census frequency rankings) -- "Michael Smith"
# and "William Harris" both fail this way without hardcoding either combo,
# and it should catch most future offenders from the same cluster without
# another full rerun. Still a heuristic, not a real fix for the underlying
# entity-resolution gap (out of scope here) -- same "targeted patch"
# philosophy as IRS_BOARD_ROLES elsewhere in this pipeline, just at the
# name-component level instead of the full-name level. Applied to both
# bridge candidates and community-label candidates below.
_COMMON_FIRST_NAMES = {
    "james", "john", "robert", "michael", "william", "david", "richard", "joseph",
    "thomas", "charles", "christopher", "daniel", "matthew", "anthony", "mark",
    "donald", "steven", "paul", "andrew", "joshua", "kenneth", "kevin", "brian",
    "george", "edward", "ronald", "timothy", "jason", "jeffrey", "ryan", "jacob",
    "gary", "nicholas", "eric", "stephen", "jonathan", "larry", "justin", "scott",
    "brandon", "mary", "patricia", "jennifer", "linda", "barbara", "susan",
    "jessica", "sarah", "karen", "nancy", "lisa", "margaret", "betty", "sandra",
    "ashley", "kimberly", "emily", "donna", "michelle", "carol", "amanda",
}
_COMMON_LAST_NAMES = {
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson", "anderson",
    "thomas", "taylor", "moore", "jackson", "martin", "lee", "perez", "thompson",
    "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill",
    "flores", "green", "adams", "nelson", "baker", "hall", "rivera", "campbell",
    "mitchell", "carter", "roberts",
}


def is_likely_name_collision(name):
    parts = name.strip().lower().split()
    if len(parts) != 2:
        return False
    first, last = parts
    return first in _COMMON_FIRST_NAMES and last in _COMMON_LAST_NAMES


def main():
    print("Loading scored graph...", flush=True)
    with gzip.open(SCORED, "rt", encoding="utf-8") as f:
        ed = json.load(f)
    nodes = ed["nodes"]
    edges = ed["edges"]
    degree = [0] * len(nodes)
    for u, v, _prob, _cats in edges:
        degree[u] += 1
        degree[v] += 1

    print("Building unweighted graph...", flush=True)
    G = nx.Graph()
    G.add_nodes_from(range(len(nodes)))
    G.add_edges_from((u, v) for u, v, _prob, _cats in edges)
    print(f"  {G.number_of_nodes()} nodes, {G.number_of_edges()} edges", flush=True)

    # --- Top by degree ---
    person_idx = [i for i in range(len(nodes))
                  if looks_like_person(nodes[i]) and degree[i] >= MIN_DEGREE
                  and not is_likely_name_collision(nodes[i])]
    person_idx.sort(key=lambda i: degree[i], reverse=True)
    top_degree = [{"name": nodes[i], "degree": degree[i]} for i in person_idx[:TOP_DEGREE_COUNT]]
    print(f"Top degree: {[(e['name'], e['degree']) for e in top_degree]}", flush=True)

    # --- Betweenness (approximate, unweighted, sampled) ---
    print(f"Computing betweenness centrality (k={BETWEENNESS_SAMPLE_K} sampled sources)...", flush=True)
    t0 = time.time()
    bc = nx.betweenness_centrality(G, k=BETWEENNESS_SAMPLE_K, seed=BETWEENNESS_SEED, normalized=True)
    print(f"  done in {time.time() - t0:.0f}s", flush=True)

    bridge_candidates = sorted(
        (i for i in range(len(nodes))
         if looks_like_person(nodes[i]) and degree[i] >= MIN_DEGREE
         and not is_likely_name_collision(nodes[i])),
        key=lambda i: bc.get(i, 0.0), reverse=True,
    )[:50]  # wider pool than TOP_BRIDGE_COUNT -- community labeling below may skip a few

    # --- Louvain communities, for labeling what each bridge connects ---
    print("Running Louvain community detection...", flush=True)
    t0 = time.time()
    comms = louvain_communities(G, weight="weight", seed=BETWEENNESS_SEED)
    print(f"  done in {time.time() - t0:.0f}s, {len(comms)} communities", flush=True)

    node_to_comm = {}
    for comm_id, members in enumerate(comms):
        for i in members:
            node_to_comm[i] = comm_id
    # Communities above this size are excluded from being used as a bridge
    # label entirely (not just filtered by name). Confirmed real problem
    # 2026-09-03: the SAME giant community (110,640 members, one of only a
    # handful this large out of 17,077 total) kept winning as "the" community
    # for every top bridge regardless of which specific name got excluded --
    # first "Michael Smith", then "William Harris" once that was blocklisted,
    # then "CLIFTONLARSONALLEN LLP" (an accounting firm; ubiquitous IRS-990
    # nonprofit-auditor connections plausibly explain why this one cluster
    # absorbs such a disproportionate, structurally-central share of the
    # graph). A 110K-member cluster isn't a coherent recognizable "world" the
    # way a real community is -- it reads as a giant weakly-connected
    # catch-all, not "the network around X". Excluding it lets the next-most-
    # connected REAL community take its place in each bridge's top-2, instead
    # of re-litigating which specific node inside the giant one gets picked.
    MAX_COMMUNITY_SIZE_FOR_LABEL = 20000
    oversized_communities = {c for c, members in enumerate(comms) if len(members) > MAX_COMMUNITY_SIZE_FOR_LABEL}
    print(f"Excluding {len(oversized_communities)} oversized communities (>{MAX_COMMUNITY_SIZE_FOR_LABEL} members) from bridge labeling", flush=True)

    # Label each community by its own highest-degree PERSON member -- a
    # numeric community ID has no name of its own; this gives a recognizable
    # stand-in ("the network around X") without a real semantic description,
    # which would need something much more involved than this pipeline does.
    comm_label_candidate = {}  # comm_id -> (best_degree, name)
    for i in range(len(nodes)):
        if not looks_like_person(nodes[i]) or is_likely_name_collision(nodes[i]):
            continue
        c = node_to_comm.get(i)
        if c is None:
            continue
        best = comm_label_candidate.get(c)
        if best is None or degree[i] > best[0]:
            comm_label_candidate[c] = (degree[i], nodes[i])

    bridges = []
    for i in bridge_candidates:
        if len(bridges) >= TOP_BRIDGE_COUNT:
            break
        own_comm = node_to_comm.get(i)
        neighbor_comm_counts = {}
        for j in G.neighbors(i):
            c = node_to_comm.get(j)
            if c is None or c == own_comm or c in oversized_communities:
                continue
            neighbor_comm_counts[c] = neighbor_comm_counts.get(c, 0) + 1
        if len(neighbor_comm_counts) < 2:
            continue  # not really bridging distinct communities, skip
        top2 = sorted(neighbor_comm_counts.items(), key=lambda x: -x[1])[:2]
        labels = []
        for comm_id, _n in top2:
            label = comm_label_candidate.get(comm_id)
            if label is None:
                labels = []
                break
            labels.append(label[1])
        if len(labels) != 2 or labels[0] == labels[1]:
            continue
        bridges.append({
            "name": nodes[i],
            "betweenness": bc.get(i, 0.0),
            "community_a": labels[0],
            "community_b": labels[1],
        })
    print(f"Bridges: {bridges}", flush=True)

    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump({"top_degree": top_degree, "bridges": bridges}, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
