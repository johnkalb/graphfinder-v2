"""Nightly network and dataset statistics for crawlie facts (2026-09-28).

Runs in rebuild_and_deploy.py after build_search_from_scored.py (cwd = the
build dir, like the other build steps) and writes webapp/data/graph_stats.json,
which build_crawlie_facts.py turns into sentences. Nothing here is served by
the webapp.

  separation  BFS from a uniform random sample of ALL people in the connected
              network (not gated on any trait -- the selection-bias trap that
              sank the per-school reach stats, see memory "education backfill").
              "Steps" = links as the site shows them in a path, so a path
              through a company or film counts it as a step.
  scale       nodes / edges / people in the scored graph
  charities   the IRS Business Master File candidates ($10M+ assets) and how
              many of them the harvester has processed so far
  law_firms   AMLAW_ROSTER attorneys per firm (only the firms we harvest)
  establishment  the "establishment" community (Leiden): size, partisan
              links, median hops, top bridges/hubs, subcommunities

The DB-backed parts are optional: DATABASE_URL from the environment, or the
gitignored secrets file via pg_secret; they're skipped if neither works.
"""
import gzip
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone

import igraph as ig
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_crawlie_facts import is_clean_label, looks_like_person  # noqa: E402

SCORED = "webapp/data/graph_scored.json.gz"
OUT = "webapp/data/graph_stats.json"
PERSON_NODES = "webapp/data/person_nodes.json.gz"   # build_person_nodes.py
CANDIDATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "irs990_asset_candidates.json")
SAMPLE_SOURCES = 300
REACH_SHARE = 0.90


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def db_connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        try:
            sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
            import pg_secret
            url = pg_secret.load_secret("DATABASE_URL")
        except Exception:
            url = None
    if not url:
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url)
        conn.cursor().execute("SET statement_timeout = '120s'")
        return conn
    except Exception as e:
        log(f"DB unavailable, skipping DB-backed stats: {e}")
        return None


def separation_stats(G, people_mask):
    comps = G.connected_components()
    giant = max(comps, key=len)
    in_giant = set(giant)
    people = [v for v in range(G.vcount()) if people_mask[v]]
    giant_people = [v for v in people if v in in_giant]
    rng = random.Random(datetime.now(timezone.utc).strftime("%Y%m%d"))
    sources = rng.sample(giant_people, min(SAMPLE_SOURCES, len(giant_people)))
    targets = np.array(giant_people)
    hist = np.zeros(1, dtype=np.int64)
    reach90 = []
    longest = 0
    t = time.time()
    for s in sources:
        d = np.asarray(G.distances(source=[s])[0], dtype=np.float64)[targets]
        d = d[d > 0].astype(np.int64)            # drop the source itself
        counts = np.bincount(d)
        if len(counts) > len(hist):
            hist = np.pad(hist, (0, len(counts) - len(hist)))
        hist[:len(counts)] += counts
        k = int(REACH_SHARE * (len(d) - 1))
        reach90.append(int(np.partition(d, k)[k]))
        longest = max(longest, int(d.max()))
    log(f"  {len(sources)} BFS in {time.time() - t:.0f}s")
    total = int(hist.sum())
    cum, within = 0, {}
    for dist, n in enumerate(hist.tolist()):
        if n:
            cum += n
            within[dist] = cum / total
    median = next(d for d in sorted(within) if within[d] >= 0.5)
    typical_reach = int(statistics.median(reach90))
    return {
        "people": len(people),
        "people_in_network": len(giant_people),
        "network_share": len(giant_people) / len(people),
        "sample_sources": len(sources),
        "median_steps": median,
        "within": {str(k): round(v, 4) for k, v in within.items() if k <= 8},
        "reach90_median_steps": typical_reach,
        "reach90_share_within_typical": round(sum(1 for r in reach90 if r <= typical_reach) / len(reach90), 3),
        "longest_seen": longest,
    }


def charity_stats(conn):
    with open(CANDIDATES, encoding="utf-8") as f:
        orgs = json.load(f)
    assets = sorted((o.get("asset_amt") or 0 for o in orgs), reverse=True)
    total = sum(assets)
    top1 = max(1, len(assets) // 100)
    largest = max(orgs, key=lambda o: o.get("asset_amt") or 0)
    out = {"count": len(orgs), "total_assets": total, "top1pct_count": top1,
           "top1pct_share": round(sum(assets[:top1]) / total, 4),
           "largest_name": largest["name"], "largest_assets": largest["asset_amt"]}
    if conn:
        cur = conn.cursor()
        cur.execute("SELECT cursor_value FROM harvest_cursors WHERE source = 'irs990_by_assets'")
        row = cur.fetchone()
        if row:
            out["processed"] = int(row[0])
    return out


def law_firm_stats(conn):
    cur = conn.cursor()
    cur.execute("SELECT target_name, count(DISTINCT source_name) FROM relationships "
                "WHERE source_data = 'AMLAW_ROSTER' GROUP BY 1 ORDER BY 2 DESC")
    rows = cur.fetchall()
    if not rows:
        return None
    return {"firms": len(rows), "attorneys": sum(n for _, n in rows),
            "largest_firm": rows[0][0], "largest_attorneys": rows[0][1]}


def inventor_stats(conn):
    """Patent co-inventor network (source_data=PATENT_COINVENTOR): inventors,
    co-inventor ties, the largest connected co-inventor group, the most
    connected inventor, and the organizations employing the most inventors.
    Names are compared lowercased -- the source mixes "SCOTT STEPHENS" and
    "Scott Stephens" styles."""
    cur = conn.cursor()
    cur.execute("SELECT source_name, target_name FROM relationships "
                "WHERE source_data = 'PATENT_COINVENTOR' AND relation_type = 'CO_INVENTOR_WITH'")
    idx, display, pairs = {}, {}, set()
    for s, t in cur.fetchall():
        if not s or not t:
            continue
        a, b = s.strip().lower(), t.strip().lower()
        if a == b:
            continue
        for key, raw in ((a, s), (b, t)):
            if key not in idx:
                idx[key] = len(idx)
                display[idx[key]] = raw.strip()
        pairs.add((min(idx[a], idx[b]), max(idx[a], idx[b])))
    g = ig.Graph(n=len(idx), edges=list(pairs), directed=False)
    comps = g.connected_components()
    degs = g.degree()
    top = max(range(g.vcount()), key=degs.__getitem__)
    top_name = display[top]
    if top_name.isupper():
        top_name = top_name.title()
    cur.execute("SELECT count(DISTINCT lower(source_name)) FROM relationships "
                "WHERE source_data = 'PATENT_COINVENTOR' AND relation_type IN ('INVENTOR_AT', 'IDENTITY')")
    inventors = max(cur.fetchone()[0], len(idx))
    cur.execute("SELECT target_name, count(DISTINCT lower(source_name)) FROM relationships "
                "WHERE source_data = 'PATENT_COINVENTOR' AND relation_type = 'INVENTOR_AT' "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 5")
    top_orgs = cur.fetchall()
    return {"inventors": inventors, "coinventor_ties": len(pairs),
            "largest_group": max(len(c) for c in comps), "groups": len(comps),
            "top_inventor": top_name, "top_inventor_coinventors": degs[top],
            "top_orgs": [[n, c] for n, c in top_orgs]}


FDIC_API = "https://banks.data.fdic.gov/api"


# --- communities (2026-10-01) ------------------------------------------------
# Leiden (modularity, weighted by edge probability) over the whole graph, then
# a closer look at the "establishment" community -- found by anchors, not by
# id, since ids change as the graph grows. Seeded so results are stable from
# night to night. Names are reported raw; build_crawlie_facts.py publishes
# only verified public figures.
ESTABLISHMENT_ANCHORS = ("Lawrence Summers", "David Rubenstein", "Council on Foreign Relations")
PARTY_ANCHORS = {"democratic": "Democratic National Committee", "republican": "Republican National Committee"}
# subcommunities held together mainly by alumni ties mix unrelated people
# (Einstein with Farrah Fawcett) -- not reported
MAX_EDUCATION_SHARE = 0.40


def community_stats(G, nodes, edges, people_mask):
    # Communities of AFFILIATION: edges whose only link is a political donation
    # are left out (2026-10-03). With donations in, the corrected graph (after
    # the LittleSis relabel) forms one 135K political-donor blob around the
    # DNC and RNC; without them, the elite core of officeholders, universities
    # and corporate leadership (112K) separates from unions, lobbying, R&D etc.
    edges = [e for e in edges if not (len(e) > 3 and set(e[3]) <= {"DONATION"})]
    G = ig.Graph(n=len(nodes), edges=[(e[0], e[1]) for e in edges], directed=False)
    random.seed(42)          # igraph draws from Python's RNG
    t = time.time()
    w = [max(float(e[2]), 1e-6) for e in edges]
    memb = G.community_leiden(objective_function="modularity", weights=w, n_iterations=4).membership
    log(f"  leiden {time.time() - t:.0f}s")
    idx = {n: i for i, n in enumerate(nodes)}
    homes = [memb[idx[a]] for a in ESTABLISHMENT_ANCHORS if a in idx]
    if len(homes) < 2 or len(set(homes)) != 1:
        log(f"  establishment anchors split or missing ({homes}) -- skipped")
        return None
    est = homes[0]
    party = {k: memb[idx[v]] for k, v in PARTY_ANCHORS.items() if v in idx}

    mem = [i for i, c in enumerate(memb) if c == est]
    S = G.induced_subgraph(mem)
    # attach weights/categories to the subgraph edges via a lookup on endpoints
    pos = {v: k for k, v in enumerate(mem)}
    sub_w, sub_cats = {}, {}
    inside_cats, links_to = {}, {}
    for e in edges:
        ca, cb = memb[e[0]], memb[e[1]]
        if ca == est and cb == est:
            key = (min(pos[e[0]], pos[e[1]]), max(pos[e[0]], pos[e[1]]))
            sub_w[key] = max(float(e[2]), 1e-6)
            sub_cats[key] = e[3] if len(e) > 3 else []
            for c in sub_cats[key]:
                inside_cats[c] = inside_cats.get(c, 0) + 1
        elif ca == est or cb == est:
            other = cb if ca == est else ca
            links_to[other] = links_to.get(other, 0) + 1
    S.es["w"] = [sub_w[tuple(sorted(e.tuple))] for e in S.es]
    is_p = [people_mask[v] for v in mem]
    P = [k for k in range(S.vcount()) if is_p[k]]
    tot_cats = sum(inside_cats.values()) or 1

    rng = random.Random(42)
    hist = {}
    pset = np.array(P)
    for s in rng.sample(P, min(200, len(P))):
        d = np.asarray(S.distances(source=[s])[0], dtype=np.float64)[pset]
        d = d[(d > 0) & np.isfinite(d)].astype(np.int64)
        for dist, n in enumerate(np.bincount(d).tolist()):
            if n:
                hist[dist] = hist.get(dist, 0) + n
    total = sum(hist.values())
    cum, median_hops = 0, None
    for dist in sorted(hist):
        cum += hist[dist]
        if median_hops is None and cum >= total / 2:
            median_hops = dist

    deg = S.degree()
    ppl_ties = [0] * S.vcount()
    for a, b in (e.tuple for e in S.es):
        if is_p[a] and is_p[b]:
            ppl_ties[a] += 1
            ppl_ties[b] += 1
    src = rng.sample(P, min(1500, len(P)))
    t = time.time()
    btw = S.betweenness(sources=src)
    log(f"  establishment betweenness {time.time() - t:.0f}s")
    name = lambda k: nodes[mem[k]]
    top_bridges = [name(k) for k in sorted(P, key=lambda k: -btw[k])[:40]]
    top_connected = [[name(k), deg[k]] for k in sorted(P, key=lambda k: -deg[k])[:40]]
    hub_orgs = [[name(k), deg[k]] for k in sorted((k for k in range(S.vcount()) if not is_p[k]),
                                                  key=lambda k: -deg[k])[:10]]

    sub = S.community_leiden(objective_function="modularity", weights="w", n_iterations=4).membership
    sub_people, sub_edu, sub_all = {}, {}, {}
    for k in P:
        sub_people[sub[k]] = sub_people.get(sub[k], 0) + 1
    for key, cats in sub_cats.items():
        a, b = key
        if sub[a] == sub[b]:
            for c in cats:
                sub_all[sub[a]] = sub_all.get(sub[a], 0) + 1
                if c == "EDUCATION":
                    sub_edu[sub[a]] = sub_edu.get(sub[a], 0) + 1
    subs = []
    for c, n in sorted(sub_people.items(), key=lambda kv: -kv[1])[:15]:
        edu = sub_edu.get(c, 0) / max(1, sub_all.get(c, 0))
        if edu > MAX_EDUCATION_SHARE:
            continue
        ks = [k for k in range(S.vcount()) if sub[k] == c]
        orgs_k = sorted((k for k in ks if not is_p[k]), key=lambda k: -deg[k])[:8]
        org_set = set(orgs_k)
        # each hub person with the hub orgs they link to DIRECTLY -- a fact may
        # only pair a person with an organization they're actually tied to
        # (clustering alone put Jair Bolsonaro "in" a BlackRock subcommunity)
        hubs = sorted((k for k in ks if is_p[k]), key=lambda k: -ppl_ties[k])[:150]
        subs.append({"people": n, "hub_orgs": [name(k) for k in orgs_k],
                     "hub_people": [[name(k), [name(o) for o in S.neighbors(k) if o in org_set]] for k in hubs]})

    return {"graph": "affiliation", "people": len(P), "orgs": S.vcount() - len(P), "edges_inside": S.ecount(),
            "edges_out": sum(links_to.values()),
            "links_democratic": links_to.get(party.get("democratic"), 0) if party.get("democratic") != est else None,
            "links_republican": links_to.get(party.get("republican"), 0) if party.get("republican") != est else None,
            "glue": [[c, round(v / tot_cats, 3)] for c, v in sorted(inside_cats.items(), key=lambda kv: -kv[1])[:5]],
            "median_hops": median_hops, "top_bridges": top_bridges, "top_connected": top_connected,
            "hub_orgs": hub_orgs, "subcommunities": subs}


def bank_stats():
    """FDIC BankFind (public, no key): active FDIC-insured institutions, how
    concentrated their assets/deposits are, and how many there were at the
    historical peak. ASSET/DEP are reported in thousands of dollars."""
    import requests
    try:
        import pip_system_certs.wrapt_requests  # noqa: F401 -- Norton TLS interception on this machine
    except Exception:
        pass
    banks, offset = [], 0
    while True:
        r = requests.get(f"{FDIC_API}/institutions", timeout=60, params={
            "filters": "ACTIVE:1", "fields": "NAME,ASSET,DEP,REPDTE", "sort_by": "ASSET",
            "sort_order": "DESC", "limit": 10000, "offset": offset})
        r.raise_for_status()
        page = [d["data"] for d in r.json()["data"]]
        banks += page
        if len(page) < 10000:
            break
        offset += 10000
    assets = [b.get("ASSET") or 0 for b in banks]
    deposits = [b.get("DEP") or 0 for b in banks]
    total_a, total_d = sum(assets), sum(deposits)
    top = banks[0]
    out = {"count": len(banks), "as_of": top.get("REPDTE"),
           "total_assets": total_a * 1000, "total_deposits": total_d * 1000,
           "top10_asset_share": round(sum(assets[:10]) / total_a, 4),
           "top10_deposit_share": round(sum(sorted(deposits, reverse=True)[:10]) / total_d, 4),
           "largest_name": top["NAME"], "largest_deposit_share": round((top.get("DEP") or 0) / total_d, 4),
           "under_1b_count": sum(1 for a in assets if a < 1_000_000)}
    # Historical commercial-bank counts. The summary has a row per state PLUS
    # national-total rows ("All States and Territories", "U.S. States and
    # DC"), so read only the all-inclusive total -- summing every row tripled
    # the 1984 count to 43,488. Savings-institution rows don't populate BANKS,
    # so this compares commercial banks with commercial banks.
    r = requests.get(f"{FDIC_API}/summary", timeout=60, params={
        "filters": 'CB_SI:CB AND STNAME:"All States and Territories"', "fields": "YEAR,BANKS", "limit": 1000})
    r.raise_for_status()
    per_year = {int(d["data"]["YEAR"]): d["data"].get("BANKS") or 0 for d in r.json()["data"]}
    if per_year:
        peak_year = max(per_year, key=per_year.get)
        latest_year = max(per_year)
        out.update({"cb_peak_year": peak_year, "cb_peak_count": per_year[peak_year],
                    "cb_latest_year": latest_year, "cb_latest_count": per_year[latest_year]})
    return out


def main():
    t0 = time.time()
    with gzip.open(SCORED, "rt", encoding="utf-8") as f:
        g = json.load(f)
    nodes = g["nodes"]
    G = ig.Graph(n=len(nodes), edges=[(e[0], e[1]) for e in g["edges"]], directed=False)
    # Same person rule as the PageRank ladder (build_search_from_scored.py), so
    # the "N people" totals in different crawlie facts agree.
    try:
        with gzip.open(PERSON_NODES, "rt", encoding="utf-8") as f:
            person_idx = set(json.load(f))
        people_mask = [i in person_idx for i in range(len(nodes))]
    except FileNotFoundError:
        log("WARNING: person_nodes.json.gz missing -- falling back to the name heuristic")
        people_mask = [looks_like_person(n) and is_clean_label(n) for n in nodes]
    log(f"graph: {G.vcount():,} nodes, {G.ecount():,} edges ({time.time() - t0:.0f}s)")

    stats = {"generated_at": datetime.now(timezone.utc).isoformat(),
             "scale": {"nodes": G.vcount(), "edges": G.ecount(), "people": sum(people_mask)}}
    stats["separation"] = separation_stats(G, people_mask)
    log(f"separation: {stats['separation']}")

    try:
        stats["establishment"] = community_stats(G, nodes, g["edges"], people_mask)
        log(f"establishment: {json.dumps(stats['establishment'])[:300]}")
    except Exception as e:
        log(f"community stats skipped: {e}")

    conn = db_connect()
    try:
        stats["charities"] = charity_stats(conn)
        log(f"charities: {stats['charities']}")
    except Exception as e:
        log(f"charity stats skipped: {e}")
    if conn:
        try:
            stats["law_firms"] = law_firm_stats(conn)
            log(f"law firms: {stats['law_firms']}")
        except Exception as e:
            log(f"law firm stats skipped: {e}")
        try:
            stats["inventors"] = inventor_stats(conn)
            log(f"inventors: {stats['inventors']}")
        except Exception as e:
            log(f"inventor stats skipped: {e}")
        conn.close()

    try:
        stats["banks"] = bank_stats()
        log(f"banks: {stats['banks']}")
    except Exception as e:
        log(f"bank stats skipped: {e}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=1, ensure_ascii=False)
    log(f"wrote {OUT} in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
