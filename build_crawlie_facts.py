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
FEC_MIDTERM = "webapp/data/fec_midterm_2026.json"     # fec_midterm_stats.py (optiplex, weekly)
# the public /demo only offers and names these (see pathfinder.py "Public demo")
VERIFIED_OUT = "webapp/data/verified_people.json.gz"
GRAPH_PEOPLE = "webapp/data/graph_people.tsv.gz"     # build_person_nodes.py
QID_MAP = os.environ.get("QID_MAP_PATH", os.path.join(os.path.expanduser("~"), "qid_map.jsonl"))
# People added straight from Wikidata (WIKIDATA_MEDIA / WIKIDATA_INFLUENCERS
# imports) already carry their QID -- Meryl Streep, MrBeast. Exported from
# those rows' evidence on 2026-10-01; re-export if the imports are rerun.
QID_IMPORTS = os.environ.get("QID_IMPORTS_PATH", os.path.join(os.path.expanduser("~"), "qid_map_imports.jsonl"))
# people public by ROLE (candidates, legislators, judges, SEC insiders,
# lobbyists) -- build_public_by_role.py; part of the public set for the demo,
# public links and query rules, but not used to pick crawlie subjects
PUBLIC_BY_ROLE = os.environ.get("PUBLIC_BY_ROLE_PATH", os.path.join(os.path.expanduser("~"), "public_by_role.json"))

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


def top_pagerank_facts(ladder):
    """The most influential PEOPLE by exact PageRank (pagerank_ladder.json
    "top", written by build_search_from_scored.py from the database's person
    typing). Until 2026-10-05 this sorted the search index's 1-100 SCI bucket
    by degree and guessed personhood from the name, which put a campaign
    committee ("WARNOCK FOR GEORGIA") at #1 and Barack Obama at #2."""
    top = (ladder or {}).get("top") or []
    facts = []
    ordinals = ["most", "second most", "third most"]
    for i, e in enumerate(top[:TOP_PAGERANK_COUNT]):
        label = ordinals[i] if i < len(ordinals) else f"{i + 1}th most"
        facts.append(fact(
            "top_pagerank",
            f"{e['name']} is the {label} influential person in the network by PageRank.",
            [str(i + 1), e["name"]], [e["name"]],
        ))
    return facts


def load_graph_people_names():
    """Display names of the graph's people (graph_people.tsv.gz, written by
    build_person_nodes.py from the database's own PERSON typing), or None."""
    try:
        with gzip.open(GRAPH_PEOPLE, "rt", encoding="utf-8") as f:
            return {line.rstrip("\n").split("\t", 1)[1] for line in f if "\t" in line}
    except FileNotFoundError:
        return None


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
            f"By raw connection count, the top {min(len(top), 10)} people are led by {top[0]['name']}, then {rest}.",
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


def share(x):
    """Like pct() but keeps small shares visible: 0.2%, not 0%."""
    return f"{x:.1%}" if x < 0.01 else pct(x)


def money(x):
    return f"${x / 1e9:.2f} billion" if x >= 1e9 else f"${x / 1e6:,.0f} million"


def midterm_facts(m):
    """2026 midterm donors by party, from fec_midterm_stats.py. Scope: House
    and Senate campaigns plus party committees; donor counts are ITEMIZED
    donors (committees need not itemize donors under $200 a cycle), totals are
    FEC's own and include unitemized gifts. No people named."""
    if not m or not m.get("parties"):
        return []
    d, r = m["parties"].get("D"), m["parties"].get("R")
    if not d or not r:
        return []
    asof = fmt_date(m["fec_file"])
    scope = "House and Senate campaigns and party committees"
    out = [
        fact("stats_midterm",
             f"So far in the 2026 midterms, {d['donors']:,} people have made itemized donations to Democratic "
             f"{scope}, and {r['donors']:,} to Republican ones, per FEC filings as of {asof}.", ["donors"]),
        fact("stats_midterm",
             f"Individuals have given {money(d['fec_individual_total'])} to Democratic {scope} in the 2026 "
             f"midterms so far, and {money(r['fec_individual_total'])} to Republican ones (FEC, as of {asof}).",
             ["totals"]),
        fact("stats_midterm",
             f"The typical 2026 midterm donation is small: the median itemized gift is ${d['median_gift']:,.0f} "
             f"to Democratic campaigns and committees and ${r['median_gift']:,.0f} to Republican ones.",
             ["median"]),
        fact("stats_midterm",
             f"Midterm money is top-heavy: the 1% of Democratic donors who gave the most ({d['top1pct_donors']:,} "
             f"people) supplied {share(d['top1pct_dollar_share'])} of the itemized money; the top 1% of Republican "
             f"donors ({r['top1pct_donors']:,}) supplied {share(r['top1pct_dollar_share'])}.", ["top1pct"]),
    ]
    if d.get("fec_committee_total") and r.get("fec_committee_total"):
        dt = d["fec_individual_total"] + d["fec_committee_total"]
        rt = r["fec_individual_total"] + r["fec_committee_total"]
        out.append(fact("stats_midterm",
                        f"PACs and other political committees have given {money(d['fec_committee_total'])} to "
                        f"Democratic {scope} in the 2026 midterms and {money(r['fec_committee_total'])} to "
                        f"Republican ones. With individuals' gifts, that's {money(dt)} for Democrats "
                        f"({pct(d['fec_committee_total'] / dt)} from PACs) and {money(rt)} for Republicans "
                        f"({pct(r['fec_committee_total'] / rt)}), per FEC filings as of {asof}.", ["pacs"]))
    for code, party, v in (("D", "Democratic", d), ("R", "Republican", r)):
        parts = []
        for b in v["buckets"]:
            label = (f"under ${b['hi']:,}" if not b["lo"] else
                     f"${b['lo']:,}+" if b["hi"] is None else f"${b['lo']:,}–{b['hi'] - 1:,}")
            parts.append(f"{label}: {share(b['donor_share'])} of donors, {share(b['dollar_share'])} of the money")
        out.append(fact("stats_midterm",
                        f"2026 {party} midterm donors (itemized) by how much they've given this cycle — "
                        + "; ".join(parts) + ".", ["distribution", code]))
    both = m.get("both")
    if both and both.get("donors"):
        out.append(fact("stats_midterm",
                        f"{both['donors']:,} people have given to both parties in the 2026 midterms — "
                        f"{money(both['to_D'])} to Democrats and {money(both['to_R'])} to Republicans.", ["both"]))
    return out + superpac_facts(m)


_STATES = dict(s.split(":") for s in (
    "AL:Alabama AK:Alaska AZ:Arizona AR:Arkansas CA:California CO:Colorado CT:Connecticut DE:Delaware "
    "FL:Florida GA:Georgia HI:Hawaii ID:Idaho IL:Illinois IN:Indiana IA:Iowa KS:Kansas KY:Kentucky "
    "LA:Louisiana ME:Maine MD:Maryland MA:Massachusetts MI:Michigan MN:Minnesota MS:Mississippi "
    "MO:Missouri MT:Montana NE:Nebraska NV:Nevada NH:New_Hampshire NJ:New_Jersey NM:New_Mexico "
    "NY:New_York NC:North_Carolina ND:North_Dakota OH:Ohio OK:Oklahoma OR:Oregon PA:Pennsylvania "
    "RI:Rhode_Island SC:South_Carolina SD:South_Dakota TN:Tennessee TX:Texas UT:Utah VT:Vermont "
    "VA:Virginia WA:Washington WV:West_Virginia WI:Wisconsin WY:Wyoming").replace("_", "\0").split())
_STATES = {k: v.replace("\0", " ") for k, v in _STATES.items()}


def fmt_date(ymd):
    d = datetime.strptime(ymd, "%Y-%m-%d")
    return f"{d:%b} {d.day}, {d.year}"


def superpac_facts(m):
    """Super PAC spending and fundraising, from fec_midterm_stats.py. Names
    only organizations (super PACs) and states, never people."""
    sp = m.get("superpacs")
    if not sp or not sp.get("spending", {}).get("total"):
        return []
    s, rc = sp["spending"], sp["receipts"]
    side = s["by_side"]
    asof = fmt_date(s["file_date"])
    out = [
        fact("stats_superpac",
             f"Super PACs have spent {money(s['total'])} on 2026 House and Senate races so far: "
             f"{money(side.get('R', 0))} helping Republicans and {money(side.get('D', 0))} helping Democrats "
             f"(FEC, as of {asof}).", ["spending"]),
        fact("stats_superpac",
             f"{pct(sum(s['attack'].values()) / s['total'])} of super PAC spending in the 2026 midterms pays for "
             f"attacks on a candidate rather than support for one.", ["attack"]),
    ]
    lean = {"D": ", pro-Democratic", "R": ", pro-Republican"}
    top = [f"{t['name']} ({money(t['spent'])}{lean.get(t['side'], '')})" for t in s["top_spenders"][:5]]
    if len(top) >= 3:
        out.append(fact("stats_superpac", "The biggest super PAC spenders in the 2026 midterms so far: "
                        + _join(top) + ".", ["top_spenders"]))

    def race(r):
        st = _STATES.get(r["state"], r["state"])
        return f"the {st} Senate race" if r["office"] == "S" else f"{r['state']}-{r['district']}'s House race"
    races = [f"{race(r)} ({money(r['spent'])})" for r in s["top_races"][:3]]
    if len(races) == 3:
        out.append(fact("stats_superpac", "Where super PACs are spending most in the 2026 midterms: "
                        + _join(races) + ".", ["races"]))
    if rc.get("dollars") and rc.get("million_donors"):
        out.append(fact("stats_superpac",
                        f"Super PACs have raised {money(rc['dollars'])} this election cycle. Just "
                        f"{rc['million_donors']:,} donors who gave $1 million or more supplied "
                        f"{pct(rc['million_share'])} of it (FEC, as of {fmt_date(m['fec_file'])}).", ["concentration"]))
        orgs = sp.get("receipts_from_orgs", 0)
        if orgs:
            out.append(fact("stats_superpac",
                            f"{pct(orgs / rc['dollars'])} of the money super PACs have raised this cycle "
                            f"({money(orgs)}) came from organizations — companies, unions, LLCs and nonprofits — "
                            f"rather than individual people.", ["orgs"]))
        campaigns = sum(p["fec_individual_total"] for p in m["parties"].values())
        big = rc["million_share"] * rc["dollars"]
        cmp = ("more than" if big > campaigns else "nearly as much as" if big >= 0.8 * campaigns
               else f"{pct(big / campaigns)} of")
        out.append(fact("stats_superpac",
                        f"{rc['million_donors']:,} donors who each gave super PACs $1 million or more have put in "
                        f"{money(big)} — {cmp} the {money(campaigns)} individuals have given all Democratic and "
                        f"Republican House and Senate campaigns and party committees combined.", ["vs_campaigns"]))
    d, r = m["parties"]["D"], m["parties"]["R"]
    if d.get("fec_committee_total") and r.get("fec_committee_total") and side.get("D") and side.get("R"):
        # given to campaigns/party committees + super PAC money spent on that
        # side's behalf; super PAC spending with no clear side is left out
        def all_in(p, v):
            return v["fec_individual_total"] + v["fec_committee_total"] + side[p]
        out.append(fact("stats_superpac",
                        f"All told, {money(all_in('D', d))} has gone to the Democratic side of the 2026 House and "
                        f"Senate midterms — {money(d['fec_individual_total'])} from individuals and "
                        f"{money(d['fec_committee_total'])} from PACs to campaigns and party committees, plus "
                        f"{money(side['D'])} super PACs spent helping them — and {money(all_in('R', r))} to the "
                        f"Republican side ({money(r['fec_individual_total'])}, {money(r['fec_committee_total'])} "
                        f"and {money(side['R'])}).", ["all_in"]))
    by = sp.get("receipts_by_side", {})
    if by.get("D", {}).get("dollars") and by.get("R", {}).get("dollars"):
        out.append(fact("stats_superpac",
                        f"Pro-Republican super PACs have raised {money(by['R']['dollars'])} this cycle, and their "
                        f"100 biggest donors supplied {pct(by['R']['top100_share'])} of it; pro-Democratic super "
                        f"PACs have raised {money(by['D']['dollars'])}, {pct(by['D']['top100_share'])} from their "
                        f"top 100.", ["by_side"]))
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


CORE_PEOPLE, CORE_WITHIN = 5, 30


def unavailable_people(est):
    """Lowercased names, among the people a core crawlie could name, whom
    Wikidata lists as dead (P570) or currently detained (a P2632 place of
    detention with no end date) -- the core is a community's living, free
    members (operator's rule, 2026-10-14: Ghislaine Maxwell is in prison,
    Ross Perot died in 2019). None if Wikidata can't be asked, and then no
    core crawlies are made: better none than one naming the dead or jailed.
    Checked live because deceased.json covers only qid_map.jsonl, not the
    imports, and the build directory's copy can be months old."""
    if not est:
        return set()
    cands = {n for n, _k in est.get("top_connected", [])[:CORE_WITHIN]}
    for sub in est.get("subcommunities", []):
        cands |= {n for n, _o in sub.get("hub_people", [])[:CORE_WITHIN]}
    qids = {}
    for path in (QID_MAP, QID_IMPORTS):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        if r["name"] in cands:
                            qids.setdefault(r["qid"], set()).add(r["name"].lower())
        except FileNotFoundError:
            pass
    if not qids:
        return set()
    try:
        import requests
        try:
            import pip_system_certs.wrapt_requests  # noqa: F401 -- Norton TLS interception on this machine
        except ImportError:
            pass
        out = set()
        ids = sorted(qids)
        for i in range(0, len(ids), 100):
            values = " ".join(f"wd:{q}" for q in ids[i:i + 100])
            query = (f"SELECT DISTINCT ?p WHERE {{ VALUES ?p {{ {values} }} "
                     "{ ?p wdt:P570 ?d } UNION { ?p p:P2632 ?s . FILTER NOT EXISTS { ?s pq:P582 ?e } } }")
            r = requests.get("https://query.wikidata.org/sparql", params={"query": query, "format": "json"},
                             headers={"User-Agent": "SixDegreesGraph/1.0 (sixdegrees.net)"}, timeout=90)
            r.raise_for_status()
            for b in r.json()["results"]["bindings"]:
                out |= qids.get(b["p"]["value"].rsplit("/", 1)[-1], set())
        print(f"Core crawlies: {len(out)} of {len(qids)} candidates dead or detained: {sorted(out)}", flush=True)
        return out
    except Exception as e:
        print(f"WARNING: Wikidata check of dead/detained people failed ({e}) -- no core crawlies", flush=True)
        return None


def _core_people(ranked, verified, excluded):
    """ranked: (name, eligible) for a community's members, best-connected
    first. The top CORE_PEOPLE eligible, verified, living and free figures
    among its CORE_WITHIN best-connected members, or [] if there aren't that
    many (or excluded is None: unknown who is dead or detained). Only verified
    people can be named, and the real top members often aren't (2026-10-01:
    naming verified ones ranked #65 and #101 as "best-connected" misled), so a
    community whose core is mostly unknown names gets no core crawlie."""
    if excluded is None:
        return []
    core = [n for n, ok in ranked[:CORE_WITHIN]
            if ok and n.lower() in verified and n.lower() not in excluded and is_clean_label(n)]
    return core[:CORE_PEOPLE] if len(core) >= CORE_PEOPLE else []


def community_facts(est, verified, sep_median=None, excluded=None):
    """The "establishment" community (build_graph_stats.community_stats).
    Every person named must be verified; ticker and posts get the same text."""
    if not est:
        return []
    # Only facts computed on the affiliation graph (donation-only links left
    # out). On the full graph the anchors' community absorbed the DNC and RNC
    # donor networks (2026-10-03) and stopped meaning "establishment".
    if est.get("graph") != "affiliation":
        print("establishment stats not from the affiliation graph -- no community facts", flush=True)
        return []
    out = []
    n = est["people"]
    glue = [_GLUE_WORDS[c] for c, _ in est.get("glue", []) if c in _GLUE_WORDS][:3]
    bridge = next((b for b in est.get("top_bridges", []) if b.lower() in verified and is_clean_label(b)), None)
    if bridge:
        out.append(fact("community_establishment",
                        f'More shortest paths between members of the "establishment" community ({n:,} people '
                        f"linked by shared {_join(glue)}) run through {bridge} than through any other "
                        f"well-known figure. {COMMUNITY_DEF}",
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
    core = _core_people([(n, True) for n, _k in est.get("top_connected", [])], verified, excluded)
    if core:
        out.append(fact("community_establishment",
                        f'The core of the "establishment" community: {_join(core)} — the best-known figures '
                        f"with the most links inside it. {COMMUNITY_DEF}", ["core"], core))
    orgs = [(short_company(o), k) for o, k in est.get("hub_orgs", []) if is_clean_label(o)]
    if len(orgs) >= 3:
        (o1, k1), (o2, k2), (o3, k3) = orgs[:3]
        out.append(fact("community_establishment",
                        f'{o1} is the biggest hub of the "establishment" community, with {k1:,} links inside it — '
                        f"ahead of {o2} ({k2:,}) and {o3} ({k3:,}). {COMMUNITY_DEF}", ["hub_org"]))
    # Subcommunities are described by their organizations; their people get a
    # crawlie of their own only when the core is well known (see _core_people).
    for sub in est.get("subcommunities", []):
        sorgs = [short_company(o) for o in sub["hub_orgs"] if is_clean_label(o)][:3]
        if len(sorgs) < 3:
            continue
        out.append(fact("community_establishment",
                        f'Inside the "establishment" community is a subcommunity of {sub["people"]:,} people '
                        f"centred on {_join(sorgs)}. {COMMUNITY_DEF}", ["sub", sorgs[0]]))
        # a separate crawlie for its core people (operator's request, 2026-10-14);
        # each must link directly to one of its hub organizations, see above
        core = _core_people([(n, bool(hub_links)) for n, hub_links in sub.get("hub_people", [])], verified, excluded)
        if core:
            out.append(fact("community_establishment",
                            f'The core of the subcommunity centred on {_join(sorgs)}: {_join(core)} — the '
                            f"best-known figures with the most ties to other people inside it. {COMMUNITY_DEF}",
                            ["core", sorgs[0]], core))
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
    # build_centrality_extras.py picks people by a name heuristic, which lets
    # "SERVICE EMPLOYEES" and "WARNOCK FOR GEORGIA" into the top-10 lists
    people_names = load_graph_people_names()
    if people_names:
        for k in ("top_degree", "top_bridge"):
            if isinstance(extras.get(k), list):
                extras[k] = [e for e in extras[k] if e.get("name") in people_names]
    verified = load_verified_people()

    ladder_ticker, ladder_public = ladder_facts(load_optional_json(PAGERANK_LADDER), verified)
    graph_stats = load_optional_json(GRAPH_STATS) or {}
    stat_records = stats_facts(graph_stats)
    stat_records += midterm_facts(load_optional_json(FEC_MIDTERM))
    stat_records += community_facts(graph_stats.get("establishment"), verified,
                                    (graph_stats.get("separation") or {}).get("median_steps"),
                                    unavailable_people(graph_stats.get("establishment")))

    records = (top_pagerank_facts(load_optional_json(PAGERANK_LADDER)) + group_facts(groups) + surprising_rank_facts(index)
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

    # the PUBLIC set (notable + public by role) for the demo, public links and
    # question rules; crawlie subjects above stay notable-only
    public = set(verified)
    try:
        with open(PUBLIC_BY_ROLE, encoding="utf-8") as f:
            public |= {n.lower() for n in json.load(f)}
    except FileNotFoundError:
        print(f"WARNING: {PUBLIC_BY_ROLE} not found -- public set is notable people only", flush=True)
    with gzip.open(VERIFIED_OUT, "wt", encoding="utf-8") as f:
        json.dump(sorted(public), f, ensure_ascii=False, separators=(",", ":"))
    print(f"Public people (notable + by role): {len(public):,}", flush=True)

    print(f"Wrote {len(records)} crawlie facts ({len(publishable)} publishable, "
          f"{len(verified)} verified public figures) to {OUT}", flush=True)
    for f in records:
        print(f"  {f['text']}", flush=True)
    print("Publishable:", flush=True)
    for f in publishable:
        print(f"  [{f['category']}] {f['text']}", flush=True)


if __name__ == "__main__":
    main()
