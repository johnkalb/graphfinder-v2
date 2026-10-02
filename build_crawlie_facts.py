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
PAGERANK_LADDER = "webapp/data/pagerank_ladder.json"   # build_search_from_scored.py
GRAPH_STATS = "webapp/data/graph_stats.json"           # build_graph_stats.py
# the public /demo only offers and names these (see pathfinder.py "Public demo")
VERIFIED_OUT = "webapp/data/verified_people.json.gz"
QID_MAP = os.environ.get("QID_MAP_PATH", os.path.join(os.path.expanduser("~"), "qid_map.jsonl"))
# People added straight from Wikidata (WIKIDATA_MEDIA / WIKIDATA_INFLUENCERS
# imports) already carry their QID -- Meryl Streep, MrBeast. Exported from
# those rows' evidence on 2026-10-01; re-export if the imports are rerun.
QID_IMPORTS = os.environ.get("QID_IMPORTS_PATH", os.path.join(os.path.expanduser("~"), "qid_map_imports.jsonl"))

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
    try:
        with open(QID_IMPORTS, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    verified.add(json.loads(line)["name"].lower())
    except FileNotFoundError:
        pass
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


def load_optional_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def ladder_facts(ladder, verified):
    """"#1 is X, #100 is Y, ..." from build_search_from_scored.py's
    pagerank_ladder.json. Two records share one key: the ticker version names
    every rung; the publishable one names only verified public figures and
    describes the rest by their connection count."""
    if not ladder or not ladder.get("rungs"):
        return [], []

    def rung_text(r, name_it):
        n = r["degree"]
        who = r["name"] if name_it else f"someone with {n:,} connection{'' if n == 1 else 's'}"
        return f"#{r['rank']:,} is {who}"

    rungs = ladder["rungs"]
    intro = f"Out of {ladder['people']:,} people in the sixdegrees network, ranked by PageRank: "
    all_names = [r["name"] for r in rungs]
    ticker = fact("pagerank_ladder", intro + "; ".join(rung_text(r, True) for r in rungs) + ".",
                  ["ladder"], all_names)
    ok = lambda r: r["name"].lower() in verified and is_clean_label(r["name"])
    public = fact("pagerank_ladder", intro + "; ".join(rung_text(r, ok(r)) for r in rungs) + ".",
                  ["ladder"], [r["name"] for r in rungs if ok(r)])
    return [ticker], [public]


def pct(x):
    return f"{x:.0%}" if x < 0.995 else f"{x:.1%}"


_COMPANY_ALIASES = {"international business machines": "IBM"}
_COMPANY_SUFFIX = re.compile(r",?\s+(?:technology licensing|technologies|corporation|corp\.?|incorporated|inc\.?"
                             r"|co\.?,? ltd\.?|ltd\.?|llc|l\.l\.c\.|gmbh|ag|s\.a\.|plc)\s*$", re.I)


def short_company(name):
    """"MICROSOFT TECHNOLOGY LICENSING, LLC" -> "Microsoft"; patent assignee
    names arrive upper-case with legal suffixes."""
    n = name.strip()
    for _ in range(3):
        n = _COMPANY_SUFFIX.sub("", n).strip(" ,")
    alias = _COMPANY_ALIASES.get(n.lower())
    if alias:
        return alias
    return n.title() if n.isupper() and len(n) > 4 else n     # keep short acronyms (CHS, IBM)


def stats_facts(stats):
    """Dataset/network statistics from build_graph_stats.py (graph_stats.json).
    No people named, so all of these are publishable."""
    if not stats:
        return []
    out = []
    sep = stats.get("separation")
    if sep:
        m = sep["median_steps"]
        out.append(fact("stats_separation",
                        "In the sixdegrees network, the median distance between two people is exactly six steps."
                        if m == 6 else
                        f"In the sixdegrees network, the median distance between two people is {m} steps — not six.",
                        ["median"]))
        w3, w6 = sep["within"].get("3"), sep["within"].get("6")
        if w3 is not None and w6 is not None:
            out.append(fact("stats_separation",
                            f"Only {pct(w3)} of pairs of people in the sixdegrees network are within 3 steps "
                            f"of each other — but {pct(w6)} are within 6.", ["within3"]))
        out.append(fact("stats_separation",
                        f"Half of all people in the sixdegrees network can reach 90% of everyone else within "
                        f"{sep['reach90_median_steps']} steps.", ["reach90"]))
        out.append(fact("stats_separation",
                        f"{pct(sep['network_share'])} of the {sep['people']:,} people in sixdegrees belong to one "
                        f"connected network — and some pairs in it are at least {sep['longest_seen']} steps apart.",
                        ["network"]))
    ch = stats.get("charities")
    if ch:
        out.append(fact("stats_charity",
                        f"{ch['count']:,} US nonprofits each hold more than $10 million in assets — "
                        f"${ch['total_assets'] / 1e12:.1f} trillion in total.", ["count"]))
        out.append(fact("stats_charity",
                        f"The largest 1% of big US nonprofits ({ch['top1pct_count']:,} organizations) hold "
                        f"{pct(ch['top1pct_share'])} of their combined assets. The biggest, "
                        f"{ch['largest_name'].title()}, holds ${ch['largest_assets'] / 1e9:,.0f} billion.",
                        ["concentration"]))
        if ch.get("processed"):
            out.append(fact("stats_charity",
                            f"sixdegrees has checked the IRS filings of {ch['processed']:,} of the {ch['count']:,} "
                            f"US nonprofits with more than $10 million in assets so far, largest first, to map their boards.",
                            ["progress"]))
    lf = stats.get("law_firms")
    if lf:
        out.append(fact("stats_law",
                        f"The {lf['firms']} global law firms tracked by sixdegrees employ {lf['attorneys']:,} "
                        f"attorneys between them; {lf['largest_firm']} alone has {lf['largest_attorneys']:,}.",
                        ["attorneys"]))
    bk = stats.get("banks")
    if bk:
        largest = bk["largest_name"].replace(", National Association", "")
        out.append(fact("stats_banks",
                        f"There are {bk['count']:,} FDIC-insured banks in the US, but the 10 largest hold "
                        f"{pct(bk['top10_asset_share'])} of all bank assets.", ["concentration"]))
        out.append(fact("stats_banks",
                        f"{largest} alone holds {bk['largest_deposit_share']:.1%} of all deposits in "
                        f"FDIC-insured US banks.", ["largest"]))
        if bk.get("cb_peak_count") and bk.get("cb_latest_count"):
            drop = 1 - bk["cb_latest_count"] / bk["cb_peak_count"]
            out.append(fact("stats_banks",
                            f"The US had {bk['cb_peak_count']:,} commercial banks in {bk['cb_peak_year']}. "
                            f"By {bk['cb_latest_year']} it had {bk['cb_latest_count']:,} — {pct(drop)} fewer.",
                            ["decline"]))
        out.append(fact("stats_banks",
                        f"{bk['under_1b_count']:,} of the {bk['count']:,} FDIC-insured US banks are community "
                        f"banks with under $1 billion in assets.", ["community"]))
    inv = stats.get("inventors")
    if inv:
        # largest_group / top_inventor are deliberately NOT published: patent
        # names aren't disambiguated, so common names ("Wei Wang": 732
        # "co-inventors") merge many people and bridge unrelated teams.
        out.append(fact("stats_inventors",
                        f"sixdegrees tracks {inv['inventors']:,} patent inventors, linked by "
                        f"{inv['coinventor_ties']:,} co-inventor ties.", ["count"]))
        orgs = [f"{short_company(n)} ({c:,})" for n, c in inv.get("top_orgs", [])[:5]]
        if len(orgs) >= 3:
            out.append(fact("stats_inventors",
                            "The companies with the most inventors in sixdegrees' patent data: "
                            + ", ".join(orgs[:-1]) + f" and {orgs[-1]}.", ["top_orgs"]))
    sc = stats.get("scale")
    if sc:
        out.append(fact("stats_scale",
                        f"sixdegrees maps {sc['nodes'] / 1e6:.2f} million people and organizations, linked by "
                        f"{sc['edges'] / 1e6:.1f} million relationships drawn from public records.", ["size"]))
    return out


# Every fact that mentions a community carries this definition (operator's
# rule, 2026-10-01): readers would otherwise take "community" in its everyday
# sense. Community nicknames ("establishment") are ours, so they go in quotes.
COMMUNITY_DEF = ('A "community" here is a cluster the software finds by itself: '
                 'people far more connected to each other than to the rest of the network.')
_GLUE_WORDS = {"CO_EXECUTIVE": "executive roles", "CO_DIRECTOR": "boards", "EDUCATION": "universities",
               "LOBBYING": "lobbying", "FAMILY": "family", "EMPLOYMENT": "employers", "DONATION": "donations",
               "MEMBERSHIP": "memberships", "FINANCIAL": "money", "CREATIVE_COLLAB": "creative work"}


def _join(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def community_facts(est, verified, sep_median=None):
    """The "establishment" community (build_graph_stats.community_stats).
    Every person named must be verified; ticker and posts get the same text."""
    if not est:
        return []
    out = []
    n = est["people"]
    glue = [_GLUE_WORDS[c] for c, _ in est.get("glue", []) if c in _GLUE_WORDS][:3]
    d, r = est.get("links_democratic"), est.get("links_republican")
    if d and r and glue:
        gap = abs(d - r) / max(d, r)
        lean = ("splits its outside ties almost evenly between Democrats and Republicans" if gap < 0.15 else
                f"leans {'Democratic' if d > r else 'Republican'} in its outside ties")
        out.append(fact("community_establishment",
                        f'The "establishment" community — {n:,} people tied together mostly by shared '
                        f"{_join(glue)} — {lean}: {d:,} links into the Democratic community, "
                        f"{r:,} into the Republican one. {COMMUNITY_DEF}", ["partisan"]))
    bridge = next((b for b in est.get("top_bridges", []) if b.lower() in verified and is_clean_label(b)), None)
    if bridge:
        out.append(fact("community_establishment",
                        f'More shortest paths between members of the "establishment" community ({n:,} people) '
                        f"run through {bridge} than through any other well-known figure. {COMMUNITY_DEF}",
                        ["bridge"], [bridge]))
    m = est.get("median_hops")
    if m and sep_median:
        if m >= sep_median:
            tail = (f"no closer than the network as a whole ({sep_median}). It is a loose web, not an inner circle."
                    if m == sep_median else f"farther apart than the network as a whole ({sep_median}).")
        else:
            tail = f"closer than the network as a whole ({sep_median})."
        out.append(fact("community_establishment",
                        f'Members of the "establishment" community ({n:,} people) are typically {m} steps apart — '
                        f"{tail} {COMMUNITY_DEF}", ["hops"]))
    orgs = [(short_company(o), k) for o, k in est.get("hub_orgs", []) if is_clean_label(o)]
    if len(orgs) >= 3:
        (o1, k1), (o2, k2), (o3, k3) = orgs[:3]
        out.append(fact("community_establishment",
                        f'{o1} is the biggest hub of the "establishment" community, with {k1:,} links inside it — '
                        f"ahead of {o2} ({k2:,}) and {o3} ({k3:,}). {COMMUNITY_DEF}", ["hub_org"]))
    # Subcommunities are described by their organizations only. Naming people
    # was tried and dropped (2026-10-01): only verified figures can be named,
    # and those ranked far down (#65, #101) while the real hubs weren't
    # verified, so "best-connected members" misled.
    for sub in est.get("subcommunities", []):
        sorgs = [short_company(o) for o in sub["hub_orgs"] if is_clean_label(o)][:3]
        if len(sorgs) < 3:
            continue
        out.append(fact("community_establishment",
                        f'Inside the "establishment" community is a subcommunity of {sub["people"]:,} people '
                        f"centred on {_join(sorgs)}. {COMMUNITY_DEF}", ["sub", sorgs[0]]))
    return out


# Categories whose key subjects are fixed identifiers ("median", "ladder"),
# not names -- only their named people get the label check.
_NON_NAME_KEYS = ("stats_", "pagerank_ladder", "community_")


def is_publishable(f, verified):
    names = list(f["people"])
    if not f["category"].startswith(_NON_NAME_KEYS):
        names += f["key"].split(":", 1)[1].split("|")
    return (all(p.lower() in verified for p in f["people"])
            and all(is_clean_label(n) for n in names if not n.isdigit()))


def main():
    index = load_search_index()
    groups = load_group_rankings()
    extras = load_centrality_extras()
    verified = load_verified_people()

    ladder_ticker, ladder_public = ladder_facts(load_optional_json(PAGERANK_LADDER), verified)
    graph_stats = load_optional_json(GRAPH_STATS) or {}
    stat_records = stats_facts(graph_stats)
    stat_records += community_facts(graph_stats.get("establishment"), verified,
                                    (graph_stats.get("separation") or {}).get("median_steps"))

    records = (top_pagerank_facts(index) + group_facts(groups) + surprising_rank_facts(index)
               + top_degree_facts(extras) + bridge_facts(extras) + ladder_ticker + stat_records)

    candidates = (records + surprising_rank_facts(index, allowed=verified)
                  + top_degree_public_fact(extras, verified) + ladder_public)
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

    with gzip.open(VERIFIED_OUT, "wt", encoding="utf-8") as f:
        json.dump(sorted(verified), f, ensure_ascii=False, separators=(",", ":"))

    print(f"Wrote {len(records)} crawlie facts ({len(publishable)} publishable, "
          f"{len(verified)} verified public figures) to {OUT}", flush=True)
    for f in records:
        print(f"  {f['text']}", flush=True)
    print("Publishable:", flush=True)
    for f in publishable:
        print(f"  [{f['category']}] {f['text']}", flush=True)


if __name__ == "__main__":
    main()
