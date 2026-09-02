"""Generate homepage "crawlie" ticker factoids from the nightly-rebuilt data.

Reads search_index.json.gz (per-person degree/SCI) and group_rankings.json.gz
(group PageRank, from build_group_rankings.py) and writes a small, ready-to-
serve list of one-line factoid sentences. Regenerated idempotently every
night -- webapp/pathfinder.py's /api/crawlie separately merges in
user_submitted_facts.json (written live by the Q&A worker, if/when that
ships) at READ time, so this script never needs to know about that file.

Output: webapp/data/crawlie_facts.json.gz
  {"generated_at": "<ISO8601>", "facts": ["sentence", ...]}
"""
import gzip
import json
import re
from datetime import datetime, timezone

SEARCH_INDEX = "webapp/data/search_index.json.gz"
GROUP_RANKINGS = "webapp/data/group_rankings.json.gz"
OUT = "webapp/data/crawlie_facts.json.gz"

MIN_DEGREE = 15          # exclude near-isolated stub nodes -- "surprising" divergence there is just noise
MAX_PAIR_FACTOIDS = 20
MAX_GROUP_PAIR_FACTOIDS = 8
TOP_PAGERANK_COUNT = 3

# Curated so generated "surprising rank" sentences reference at least one name
# a homepage visitor will actually recognize -- pairing two obscure names
# means nothing to a reader even if the divergence is real. Mirrors the
# sanity-check name list in build_search_from_scored.py.
FAMOUS_NAMES = ["Donald Trump", "Jeffrey Epstein", "Gavin Newsom", "Barack Obama",
                "Hillary Clinton", "Bill Clinton"]

# Same org-word heuristic as build_scored_edges.py's looks_like_person() --
# don't invent a second one.
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


def load_search_index():
    with gzip.open(SEARCH_INDEX, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_group_rankings():
    try:
        with gzip.open(GROUP_RANKINGS, "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def surprising_rank_facts(index):
    people = [e for e in index if looks_like_person(e["canonical"]) and e["degree"] >= MIN_DEGREE]
    if len(people) < 2:
        return []

    by_degree = sorted(people, key=lambda e: e["degree"], reverse=True)
    by_sci = sorted(people, key=lambda e: e["sci"], reverse=True)
    degree_rank = {e["canonical"]: i for i, e in enumerate(by_degree)}
    sci_rank = {e["canonical"]: i for i, e in enumerate(by_sci)}
    n = len(people)

    # Positive divergence = high PageRank relative to degree rank (i.e. ranks
    # much better on SCI than on raw degree) -- the "fewer contacts, higher
    # rank" case the factoid format is built around.
    divergence = []
    for e in people:
        name = e["canonical"]
        d_norm = degree_rank[name] / n
        s_norm = sci_rank[name] / n
        divergence.append((d_norm - s_norm, e))
    divergence.sort(key=lambda t: t[0], reverse=True)
    pool = [e for _score, e in divergence[:200]]

    famous_by_name = {e["canonical"]: e for e in index if e["canonical"] in FAMOUS_NAMES}

    facts = []
    used_names = set()
    for entry in pool:
        if len(facts) >= MAX_PAIR_FACTOIDS:
            break
        name = entry["canonical"]
        if name in used_names or name in FAMOUS_NAMES:
            continue
        # Pair against the closest-degree famous name with meaningfully lower SCI.
        candidates = [f for f in famous_by_name.values()
                      if f["degree"] >= entry["degree"] and f["sci"] < entry["sci"]]
        if not candidates:
            continue
        partner = min(candidates, key=lambda f: abs(f["degree"] - entry["degree"]))
        facts.append(
            f"{name} has {entry['degree']:,} contacts and outranks {partner['canonical']} "
            f"({partner['degree']:,} contacts) in PageRank — it's who you know."
        )
        used_names.add(name)
        used_names.add(partner["canonical"])
    return facts


def group_facts(groups):
    # Cross-category PageRank comparisons ("X outranks Y"), mirroring
    # surprising_rank_facts()'s phrasing for individuals -- a standalone
    # "reaches N people, Nth percentile" sentence (the original design) reads
    # as an isolated stat with nothing to anchor it to, which is exactly what
    # the user found confusing 2026-09-02; a comparison against a DIFFERENT
    # kind of organization is more legible and mirrors what the ticker
    # already does for people.
    # Rank/pair by raw pagerank, NOT percentile: unlike individuals (where
    # only the coarse 1-100 sci bucket is persisted), each group's real
    # pagerank float IS available here -- and it turns out to matter. Real
    # 2026-09-02 output: 17 of 18 groups landed at percentile=100 (a handful
    # of high-reach synthetic nodes among ~847K real ones all round into the
    # top bucket), which would make "closest percentile" pairing pick an
    # essentially arbitrary partner and an arbitrary higher/lower call among
    # ties -- even though the real pageranks are clearly differentiated (e.g.
    # Ford Motor Co 0.000108 vs Fox Corp 0.000007, ~15x apart, both "100th
    # percentile"). Sorting/pairing by the raw float instead fixes this.
    facts = []
    ranked = sorted(groups, key=lambda g: g["pagerank"], reverse=True)
    used = set()
    for g in ranked:
        if len(facts) >= MAX_GROUP_PAIR_FACTOIDS:
            break
        if g["name"] in used:
            continue
        candidates = [
            o for o in ranked
            if o["name"] not in used and o["name"] != g["name"] and o["category"] != g["category"]
        ]
        if not candidates:
            continue
        partner = min(candidates, key=lambda o: abs(o["pagerank"] - g["pagerank"]))
        higher, lower = (g, partner) if g["pagerank"] >= partner["pagerank"] else (partner, g)
        facts.append(
            f"The {higher['name']} (reaches {higher['external_neighbor_count']:,} people outside "
            f"itself) outranks the {lower['name']} ({lower['external_neighbor_count']:,} people) "
            f"in PageRank — {higher['category'].replace('_', ' ')} vs {lower['category'].replace('_', ' ')}."
        )
        used.add(g["name"])
        used.add(partner["name"])
    return facts


def top_pagerank_facts(index):
    # search_index.json.gz only persists `sci` as a 1-100 PERCENTILE bucket, not
    # the raw PageRank score -- thousands of nodes tie at sci=100, so sorting by
    # sci alone picks an arbitrary tied entry, not the true #1 (confirmed
    # 2026-08-31: real output surfaced "116 EAST 65TH STREET LLC" and two "3M"
    # entities as the "top 3", an SEC-filer-address LLC and a company, not
    # people -- also missing the looks_like_person() filter every other
    # category here applies). Filtering to people first, then using degree as
    # a secondary sort key within the percentile tie, is a real improvement but
    # still an approximation -- it is NOT guaranteed to recover the true
    # highest-PageRank person among a same-percentile tie, since degree and
    # PageRank don't always agree. Good enough for a homepage ticker sentence.
    people = [e for e in index if looks_like_person(e["canonical"])]
    ranked = sorted(people, key=lambda e: (e["sci"], e["degree"]), reverse=True)
    facts = []
    ordinals = ["most", "second most", "third most"]
    for i, e in enumerate(ranked[:TOP_PAGERANK_COUNT]):
        label = ordinals[i] if i < len(ordinals) else f"{i + 1}th most"
        facts.append(f"{e['canonical']} is the {label} influential node in the entire network by PageRank.")
    return facts


def main():
    index = load_search_index()
    groups = load_group_rankings()

    facts = top_pagerank_facts(index) + group_facts(groups) + surprising_rank_facts(index)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
    }
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(facts)} crawlie facts to {OUT}", flush=True)
    for fact in facts:
        print(f"  {fact}", flush=True)


if __name__ == "__main__":
    main()
