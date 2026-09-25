"""Generate homepage "crawlie" ticker factoids from the nightly-rebuilt data.

Reads search_index.json.gz (per-person degree/SCI) and group_rankings.json.gz
(group PageRank, from build_group_rankings.py) and writes a small, ready-to-
serve list of one-line factoid sentences. Regenerated idempotently every
night -- webapp/pathfinder.py's /api/crawlie separately merges in
user_submitted_facts.json (written live by the Q&A worker, if/when that
ships) at READ time, so this script never needs to know about that file.

Output: webapp/data/crawlie_facts.json.gz
  {"generated_at": "<ISO8601>",
   "facts": ["sentence", ...],                       # homepage ticker
   "publishable": [{"key", "category", "text"}, ...]} # safe to broadcast

"publishable" is the subset fit for posting outside the Cloudflare gate (the
Fediverse crawlie account). A post is permanent and copied to other servers,
so every person it names must be a verified public figure -- a confirmed
Wikidata identity from qid_resolver_incremental.py's qid_map.jsonl, or one
of FAMOUS_NAMES -- and no ALL-CAPS filing-style labels ("SERVICE
EMPLOYEES"). `key` identifies a fact independent of its numbers, so a poster
can tell a genuinely new fact from last night's with updated counts.
"""
import gzip
import json
import os
import re
from datetime import datetime, timezone

SEARCH_INDEX = "webapp/data/search_index.json.gz"
GROUP_RANKINGS = "webapp/data/group_rankings.json.gz"
CENTRALITY_EXTRAS = "webapp/data/centrality_extras.json.gz"
OUT = "webapp/data/crawlie_facts.json.gz"
QID_MAP = os.environ.get("QID_MAP_PATH", os.path.join(os.path.expanduser("~"), "qid_map.jsonl"))

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


def load_verified_people():
    """Lowercased names with a confirmed Wikidata identity, plus FAMOUS_NAMES.
    FAMOUS_NAMES is needed because the resolver currently fails to confirm
    the very top names (Obama, Trump -- attempted, not confirmed, 2026-09-25)."""
    verified = {n.lower() for n in FAMOUS_NAMES}
    try:
        with open(QID_MAP, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    verified.add(json.loads(line)["name"].lower())
    except FileNotFoundError:
        print(f"WARNING: {QID_MAP} not found -- publishable facts limited to FAMOUS_NAMES", flush=True)
    return verified


def is_clean_label(name):
    # ALL-CAPS names are filing-style org labels ("SERVICE EMPLOYEES",
    # "DELL PRODUCTS L.P.") and all-lowercase ones are unnormalized source
    # names ("dan patrick") -- fine scrolling past on the ticker, not in a post.
    letters = [c for c in name if c.isalpha()]
    return (bool(letters) and not all(c.isupper() for c in letters)
            and not all(c.islower() for c in letters))


def fact(category, text, subjects, people=()):
    """subjects: names that identify the fact (for its key); people: the
    individuals it names, which must all be verified for it to be published."""
    return {"category": category, "text": text,
            "key": f"{category}:" + "|".join(subjects), "people": list(people)}


def load_search_index():
    with gzip.open(SEARCH_INDEX, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_group_rankings():
    try:
        with gzip.open(GROUP_RANKINGS, "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def load_centrality_extras():
    # build_centrality_extras.py is allowed to fail without aborting the
    # pipeline (see its own module docstring -- it's a ~20min approximate
    # computation, strictly less load-bearing than PageRank itself), so this
    # file may legitimately not exist yet on a given run. Same
    # graceful-absence handling as load_group_rankings().
    try:
        with gzip.open(CENTRALITY_EXTRAS, "rt", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"top_degree": [], "bridges": []}


def surprising_rank_facts(index, allowed=None):
    # allowed: optional set of lowercased names to restrict subjects to (the
    # publishable variant) -- otherwise the pool is dominated by obscure,
    # likely-private people, which is fine for the gated ticker only.
    people = [e for e in index if looks_like_person(e["canonical"]) and e["degree"] >= MIN_DEGREE
              and (allowed is None or e["canonical"].lower() in allowed)]
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

    if allowed is None:
        famous_by_name = {e["canonical"]: e for e in index if e["canonical"] in FAMOUS_NAMES}
    else:
        # Publishable variant: any verified public figure can be the partner,
        # each used once -- FAMOUS_NAMES alone pairs nearly every fact with
        # Gavin Newsom (the only one with a small enough degree).
        famous_by_name = {e["canonical"]: e for e in people}

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
                      if f["degree"] >= entry["degree"] and f["sci"] < entry["sci"]
                      and (allowed is None or (f["canonical"] not in used_names
                                               and f["degree"] >= 2 * entry["degree"]))]
        if not candidates:
            continue
        if allowed is None:
            partner = min(candidates, key=lambda f: abs(f["degree"] - entry["degree"]))
        else:
            # Best-connected eligible partner -- the comparison only lands if
            # the reader recognizes who's being outranked.
            partner = max(candidates, key=lambda f: f["degree"])
        facts.append(fact(
            "who_you_know",
            f"{name} has {entry['degree']:,} contacts and outranks {partner['canonical']} "
            f"({partner['degree']:,} contacts) in PageRank — it's who you know.",
            [name, partner["canonical"]], [name, partner["canonical"]],
        ))
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
        # Groups are hand-curated (build_group_rankings.py), so no people to verify.
        facts.append(fact(
            "group_vs_group",
            f"The {higher['name']} (reaches {higher['external_neighbor_count']:,} people outside "
            f"itself) outranks the {lower['name']} ({lower['external_neighbor_count']:,} people) "
            f"in PageRank — {higher['category'].replace('_', ' ')} vs {lower['category'].replace('_', ' ')}.",
            [higher["name"], lower["name"]],
        ))
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
        facts.append(fact(
            "top_pagerank",
            f"{e['canonical']} is the {label} influential node in the entire network by PageRank.",
            [str(i + 1), e["canonical"]], [e["canonical"]],
        ))
    return facts


def top_degree_facts(extras):
    # Raw connection count -- a genuinely different measure from PageRank
    # ("who you know" vs. "how many you know"), already computed and
    # person-filtered by build_centrality_extras.py; this function just
    # phrases it.
    facts = []
    top = extras.get("top_degree", [])
    if not top:
        return facts
    facts.append(fact(
        "top_degree",
        f"{top[0]['name']} has the most direct contacts in the entire network: {top[0]['degree']:,}.",
        [top[0]["name"]], [top[0]["name"]],
    ))
    if len(top) > 1:
        rest = ", ".join(f"{e['name']} ({e['degree']:,})" for e in top[1:10])
        facts.append(fact(
            "top_degree_list",
            f"By raw connection count, the top 10 are led by {top[0]['name']}, then {rest}.",
            [e["name"] for e in top[:10]], [e["name"] for e in top[:10]],
        ))
    return facts


def top_degree_public_fact(extras, verified):
    # Publishable variant of the top-10 list: build_centrality_extras.py's
    # person filter lets orgs through ("SERVICE EMPLOYEES", "DLA Piper"), so
    # list only verified public figures rather than drop the fact entirely.
    top = [e for e in extras.get("top_degree", []) if e["name"].lower() in verified]
    if len(top) < 3:
        return []
    names = ", ".join(f"{e['name']} ({e['degree']:,})" for e in top)
    return [fact("top_degree_public",
                 f"Most-connected public figures in the network, by direct contacts: {names}.",
                 [e["name"] for e in top], [e["name"] for e in top])]


def bridge_facts(extras):
    # "Bridges" = high-betweenness people -- who sits on the most shortest
    # paths between OTHER people, a structural-broker measure distinct from
    # both PageRank (importance) and degree (popularity). See
    # build_centrality_extras.py's module docstring for the approximation
    # this is built on (sampled, unweighted betweenness) and how
    # community_a/community_b are derived (Louvain partition, each labeled
    # by its own highest-degree member since a numeric community has no name
    # of its own).
    facts = []
    for b in extras.get("bridges", []):
        # Community labels are just each community's highest-degree member --
        # often an org or a private person -- so they count as named people.
        facts.append(fact(
            "bridge",
            f"{b['name']} is one of the network's biggest bridges — connecting the world of "
            f"{b['community_a']} with the world of {b['community_b']}.",
            [b["name"]], [b["name"], b["community_a"], b["community_b"]],
        ))
    return facts


def is_publishable(f, verified):
    names = f["people"] + f["key"].split(":", 1)[1].split("|")
    return (all(p.lower() in verified for p in f["people"])
            and all(is_clean_label(n) for n in names if not n.isdigit()))


def main():
    index = load_search_index()
    groups = load_group_rankings()
    extras = load_centrality_extras()
    verified = load_verified_people()

    records = (top_pagerank_facts(index) + group_facts(groups) + surprising_rank_facts(index)
               + top_degree_facts(extras) + bridge_facts(extras))

    candidates = (records + surprising_rank_facts(index, allowed=verified)
                  + top_degree_public_fact(extras, verified))
    publishable, seen = [], set()
    for f in candidates:
        if f["key"] not in seen and is_publishable(f, verified):
            seen.add(f["key"])
            publishable.append({k: f[k] for k in ("key", "category", "text")})

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "facts": [f["text"] for f in records],
        "publishable": publishable,
    }
    with gzip.open(OUT, "wt", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(records)} crawlie facts ({len(publishable)} publishable, "
          f"{len(verified)} verified public figures) to {OUT}", flush=True)
    for f in records:
        print(f"  {f['text']}", flush=True)
    print("Publishable:", flush=True)
    for f in publishable:
        print(f"  [{f['category']}] {f['text']}", flush=True)


if __name__ == "__main__":
    main()
