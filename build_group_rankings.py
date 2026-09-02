"""Compute group PageRank for manually-curated groups (e.g. "the board of Org X").

A group's score is the PageRank of a single synthetic "quotient" node wired to
the union of its members' EXTERNAL contacts (intra-group edges excluded, so a
board's own internal density doesn't inflate its own reach score). This
correctly avoids double-counting shared connections and is recursive/weighted
via PageRank, unlike naively summing members' individual scores.

Two-pass design, deliberately: pass 1 (in build_search_from_scored.py) stays
completely unperturbed so real people's SCI percentiles never shift because of
this feature. This script independently loads the same graph_scored.json.gz,
builds its own copy, adds ALL groups' synthetic nodes in one batch, and runs
PageRank ONCE for all of them together -- not once per group.

Board-role classification deliberately does NOT use webapp/relation_categories.py's
categorize() for IRS-990-sourced rows: that source writes relation_type as the
literal string "POSITION (<role>)" (e.g. "POSITION (DIRECTOR)"), which
categorize() misclassifies -- "POSITION (DIRECTOR)" contains "CTO" as a
substring ("dire-CTO-r") and false-triggers the CO_EXECUTIVE keyword check,
and casing/abbreviation variants ("POSITION (Dir)", "POSITION (VICE PRESIDE)")
fall through to OTHER entirely. This is a real, separate, pre-existing
data-quality bug (structurally identical to the FEC relation_type bug fixed
2026-08-29) -- this script works around it locally by matching the raw IRS
relation_type against its own whitelist below, rather than fixing
relation_categories.py or backfilling the DB.

Output: webapp/data/group_rankings.json.gz
  [{"name", "member_count", "external_neighbor_count", "pagerank", "percentile",
    "members": [...]}, ...]
"""
import bisect
import gzip
import json
import os
import sqlite3
import sys

import networkx as nx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))
from relation_categories import categorize

# Absolute canonical path, deliberately NOT the relative "data/pipeline_cache.db"
# hardlinked copy under graphfinder-clean/. That hardlink shares the main .db
# file's bytes but NOT its -wal/-shm sidecars, so a connection opened via the
# graphfinder-clean path gets its own separate WAL/SHM pair pointed at the same
# underlying pages -- SQLite's WAL locking can't coordinate across that split,
# so a live harvester's checkpoint on the canonical side can race a read here
# and produce "database disk image is malformed" (hit in practice 2026-08-31,
# ~25min into a run, right as it reached its first DB query -- structurally
# the same root cause as the earlier per-directory WAL divergence bug this
# session, just via this script's own connection instead of the rebuild
# pipeline's). Connecting to the canonical path directly uses the SAME wal/shm
# every harvester writes through, which is what WAL mode is actually designed
# to make safe.
DB = os.environ.get("DB_PATH", "C:/Users/johnk/data/pipeline_cache.db")
SCORED = "webapp/data/graph_scored.json.gz"
GROUPS = "group_definitions.json"
OUT = "webapp/data/group_rankings.json.gz"

# IRS_990_TEOS/IRS_990 write relation_type as "POSITION (<ROLE>)" -- match the
# role text directly rather than trusting categorize() on this source (see
# module docstring). Staff-officer titles are deliberately excluded from
# "board" -- a treasurer or secretary isn't a director -- unless a future
# group definition explicitly wants to include them.
IRS_BOARD_ROLES = {"DIRECTOR", "DIR", "TRUSTEE", "CO-TRUSTEE", "BOARD MEMBER", "CHAIRMAN", "CHAIR"}

# For non-IRS sources (LittleSis/SEC/AmLaw/etc.), categorize() is reliable --
# accept the board-equivalent category first.
BOARD_CATEGORIES = {"CO_DIRECTOR"}

# Fallback cascade for orgs with NO CO_DIRECTOR-category matches -- confirmed
# 2026-09-02 that CO_DIRECTOR-only silently excludes entire org types this
# dataset legitimately covers differently:
#   - Nonprofits/foundations sourced via LITTLESIS (not IRS_990) use "TRUSTEE"-
#     family relation_types, which categorize() maps to ADVISORY, not
#     CO_DIRECTOR (e.g. Ford Foundation: 8 TRUSTEE + 2 MEMBER_BOARD_OF_TRUSTEES
#     rows, zero CO_DIRECTOR rows).
#   - Private companies / hedge funds (Millennium, Point72, Citadel, Anthropic)
#     have no SEC "board of directors" concept at all in this data -- their
#     real senior-leadership rows (FOUNDER, CEO, CHAIRMAN_AND_CHIEF_EXECUTIVE_
#     OFFICER, ...) categorize() maps to CO_EXECUTIVE.
# CO_EXECUTIVE is deliberately NOT tried first/always: for LittleSis-sourced
# orgs the bare relation_type "POSITION" (no role text at all -- confirmed via
# the evidence field, e.g. "David Fife had a position (Position) at Point72" --
# LittleSis's own source data never captured the actual title) is the single
# largest bucket for nearly every org checked and ALSO categorizes as
# CO_EXECUTIVE, so trying it unconditionally would flood small/private orgs
# with "was affiliated somehow" noise instead of real leadership. Excluding
# that literal string keeps the fallback meaningful.
FALLBACK_CATEGORIES = {"ADVISORY", "CO_EXECUTIVE"}
_UNINFORMATIVE_RELATION_TYPES = {"POSITION"}


def irs_role(relation_type):
    """Extract the role text from "POSITION (ROLE)"; None if not that shape."""
    rt = (relation_type or "").strip()
    if rt.upper().startswith("POSITION (") and rt.endswith(")"):
        return rt[len("POSITION ("):-1].strip().upper()
    return None


def assemble_board(conn, target_org):
    """Return the deduped set of person-names who hold a board/leadership-type
    relation to target_org, per the classification rules in the module
    docstring. Tries CO_DIRECTOR (real boards) first; only falls back to
    ADVISORY/CO_EXECUTIVE (trustees / named senior leadership) when that
    yields nothing, so well-covered public companies keep the cleanest signal
    while private companies and LittleSis-sourced nonprofits still resolve."""
    director_members = set()
    fallback_members = set()
    # target_type='ORG' is redundant in principle (target_name alone
    # identifies the org) but critical in practice: it lets SQLite use the
    # (target_type, target_name) composite index for an equality lookup
    # instead of a near-full-table scan. Confirmed 2026-09-02 -- omitting it
    # was always the query's shape, but only became a real cost once this
    # script grew from 1 group to 18: 18 unindexed scans over 124M+ rows,
    # stacked on the already-slow graph-copy phase, blew past the pipeline's
    # 3600s per-step timeout. See check_org_board_coverage.py (repo root) for
    # the diagnostic that already validated this exact indexed shape is fast.
    cur = conn.execute(
        "SELECT source_name, relation_type, source_data FROM relationships "
        "WHERE target_name = ? AND target_type = 'ORG' AND source_type = 'PERSON'",
        (target_org,),
    )
    for source_name, relation_type, source_data in cur:
        if not source_name:
            continue
        if source_data in ("IRS_990_TEOS", "IRS_990"):
            role = irs_role(relation_type)
            if role in IRS_BOARD_ROLES:
                director_members.add(source_name)
            continue
        cat = categorize(relation_type)
        if cat in BOARD_CATEGORIES:
            director_members.add(source_name)
        elif cat in FALLBACK_CATEGORIES and (relation_type or "").strip().upper() not in _UNINFORMATIVE_RELATION_TYPES:
            fallback_members.add(source_name)
    return director_members or fallback_members


def main():
    with open(GROUPS, "r", encoding="utf-8") as f:
        group_defs = json.load(f)
    if not group_defs:
        print("No groups configured -- writing empty output.", flush=True)
        with gzip.open(OUT, "wt", encoding="utf-8") as f:
            json.dump([], f)
        return

    print("Loading scored graph...", flush=True)
    with gzip.open(SCORED, "rt", encoding="utf-8") as f:
        ed = json.load(f)
    nodes = ed["nodes"]
    edges = ed["edges"]
    name_to_idx = {name: i for i, name in enumerate(nodes)}

    print("Building graph copy for group-node injection...", flush=True)
    g2 = nx.Graph()
    g2.add_nodes_from(nodes)
    neighbors_by_idx = {}
    for u, v, prob, _cats in edges:
        g2.add_edge(nodes[u], nodes[v], weight=float(prob))
        neighbors_by_idx.setdefault(u, set()).add(v)
        neighbors_by_idx.setdefault(v, set()).add(u)

    conn = sqlite3.connect(DB)
    groups = []
    for gdef in group_defs:
        name = gdef["name"]
        if gdef.get("mode") == "org_lookup":
            members = assemble_board(conn, gdef["target_org"])
        elif gdef.get("mode") == "member_list":
            members = set(gdef.get("members", []))
        else:
            print(f"  skipping group {name!r}: unknown mode {gdef.get('mode')!r}", flush=True)
            continue

        if not members:
            print(f"  skipping group {name!r}: no members resolved", flush=True)
            continue

        # Union of external neighbors, excluding intra-group edges.
        external = set()
        for member in members:
            idx = name_to_idx.get(member)
            if idx is None:
                continue
            for neighbor_idx in neighbors_by_idx.get(idx, ()):
                neighbor_name = nodes[neighbor_idx]
                if neighbor_name not in members:
                    external.add(neighbor_name)

        if not external:
            print(f"  skipping group {name!r}: no external neighbors found", flush=True)
            continue

        synthetic_name = f"__GROUP__:{name}"
        g2.add_node(synthetic_name)
        for neighbor_name in external:
            # Synthetic edges aren't scored by link_scoring.py's noisy-OR (no
            # relation categories to combine) -- use a fixed representative
            # weight. Absolute score isn't the point; relative rank position
            # of one group vs another (or vs a person) is.
            g2.add_edge(synthetic_name, neighbor_name, weight=1.0)

        groups.append({
            "name": name,
            "category": gdef.get("category", "other"),
            "synthetic_name": synthetic_name,
            "member_count": len(members),
            "external_neighbor_count": len(external),
            "members": sorted(members),
        })

    conn.close()

    if not groups:
        print("No groups resolved to any members -- writing empty output.", flush=True)
        with gzip.open(OUT, "wt", encoding="utf-8") as f:
            json.dump([], f)
        return

    print(f"Running PageRank (pass 2, {len(groups)} synthetic group nodes added)...", flush=True)
    pr = nx.pagerank(g2, weight="weight", max_iter=200)

    # Percentile against pass-2's own distribution. Near-identical in shape to
    # pass-1's (build_search_from_scored.py) since only a handful of synthetic
    # nodes join an ~836K-node graph -- treat cross-comparison to an
    # individual's SCI as a close approximation, not an exact match.
    all_scores = sorted(pr.values())
    num_nodes = len(all_scores)

    def percentile_of(score):
        idx = bisect.bisect_left(all_scores, score)
        return max(1, min(100, round((idx / num_nodes) * 100)))

    for group in groups:
        score = pr.get(group["synthetic_name"], 0.0)
        group["pagerank"] = score
        group["percentile"] = percentile_of(score)
        del group["synthetic_name"]

    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(groups)} group rankings to {OUT}", flush=True)
    for group in groups:
        print(f"  {group['name']}: {group['member_count']} members, "
              f"{group['external_neighbor_count']} external contacts, "
              f"pagerank={group['pagerank']:.6f}, percentile={group['percentile']}", flush=True)


if __name__ == "__main__":
    main()
