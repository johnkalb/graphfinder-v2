"""Congress tie-strength pilot (docs/specs/congress_tie_strength.md, 2026-11-18).

How likely two members of Congress (1993 onward) are to know each other, from
the contexts they shared: committees and subcommittees (sized, per Congress),
leadership, state delegation, entering class, caucuses, years together, party,
ideology (DW-NOMINATE) and co-sponsored bills. A logistic model is fitted
against independent evidence of real ties (LittleSis social/professional/family
links, Haiku-confirmed news ties, Wikidata family) and the scored pairs feed the
nightly graph build (build_scored_edges.py reads cg_tie) plus the path
explanations (webapp/data/congress_ties.json.gz).

  python congress_ties.py load      # raw files -> in-memory model -> cache (data/congress_model.pkl.gz)
  python congress_ties.py match     # members -> graph nodes (report)
  python congress_ties.py fit       # features, labels, model report
  python congress_ties.py export    # cg_* tables + cg_tie_candidate (staging) in Postgres
  python congress_ties.py publish   # cg_tie_candidate -> cg_tie, which the nightly build reads
  python congress_ties.py all       # load .. export (not publish)

Raw inputs live in data/raw/congress (downloaded 2026-11-18; see the spec's
source table). Nothing here touches the live site; the graph changes only when
the nightly build picks up cg_tie.
"""
import argparse
import collections
import csv
import glob
import gzip
import io
import json
import math
import os
import pickle
import random
import re
import sys
import zipfile
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw", "congress")
CACHE = os.path.join(HERE, "data", "congress_model.pkl.gz")
GRAPH = os.path.join(HERE, "webapp", "data", "graph_scored.json.gz")
EXPLAIN_OUT = os.path.join(HERE, "webapp", "data", "congress_ties.json.gz")
REPORT = os.path.join(HERE, "data", "congress_ties_report.txt")
TIES_LOCAL = os.path.join(HERE, "data", "congress_ties_candidate.pkl.gz")
FIRST_CONGRESS = 103                 # 1993
CURRENT_CONGRESS = 119               # 2025-2027; later term years are in the future
SMALL_BILL = 25                      # bills with at most this many sponsors+cosponsors count as close work
LEAD_BILL_MAX = 100                  # sponsor -> cosponsor pairs counted on bills up to this size
MODEL_VERSION = "2026-11-18.1"
STRONG_P, WEAK_P, P_CAP = 0.5, 0.2, 0.75          # from the fit report (test precision 0.83 / 0.67)

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")   # pg_secret


def log(*a):
    print(*a, flush=True)


def congress_of(d):
    """The Congress in session on date d (each starts 3 January of an odd year)."""
    y = d.year - (1 if (d.month == 1 and d.day < 3) else 0)
    return (y - 1789) // 2 + 1


def congress_years(c):
    s = 1789 + 2 * (c - 1)
    return s, s + 2


def years_label(cs):
    """[116, 117] -> '2019–2023'; non-consecutive runs joined with commas."""
    cs = sorted(set(cs))
    runs, start, prev = [], cs[0], cs[0]
    for c in cs[1:] + [None]:
        if c is not None and c == prev + 1:
            prev = c
            continue
        runs.append(f"{congress_years(start)[0]}–{congress_years(prev)[1]}")
        if c is not None:
            start = prev = c
    return ", ".join(runs)


def _d(x):
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


# --------------------------------------------------------------------------- members

def load_members():
    import yaml
    members = {}
    for fn in ("legislators-historical.yaml", "legislators-current.yaml"):
        with open(os.path.join(RAW, fn), encoding="utf-8") as f:
            for m in yaml.safe_load(f):
                ids, nm = m.get("id", {}), m.get("name", {})
                terms = []
                for t in m.get("terms", []):
                    s, e = _d(t["start"]), _d(t["end"])
                    chamber = "H" if t["type"] == "rep" else "S"
                    last = min(congress_of(e - __import__("datetime").timedelta(days=1)), CURRENT_CONGRESS)
                    for c in range(congress_of(s), last + 1):
                        terms.append((c, chamber, t.get("state"), t.get("district"), t.get("party") or ""))
                if not terms or max(c for c, *_ in terms) < FIRST_CONGRESS:
                    continue
                full = nm.get("official_full") or " ".join(x for x in (nm.get("first"), nm.get("last")) if x)
                members[ids["bioguide"]] = {
                    "bioguide": ids["bioguide"], "name": full, "first": nm.get("first", ""),
                    "middle": nm.get("middle", ""), "last": nm.get("last", ""), "nick": nm.get("nickname", ""),
                    "suffix": nm.get("suffix", ""), "icpsr": ids.get("icpsr"), "qid": ids.get("wikidata"),
                    "fec": ids.get("fec") or [], "terms": terms}
    log(f"members serving in the {FIRST_CONGRESS}th Congress or later: {len(members):,}")
    return members


def load_voteview(members):
    """DW-NOMINATE dim 1 per (bioguide, congress) and an ICPSR -> bioguide map."""
    icpsr = {m["icpsr"]: b for b, m in members.items() if m["icpsr"]}
    nom = {}
    with open(os.path.join(RAW, "voteview_members.csv"), encoding="utf-8") as f:
        for r in csv.DictReader(f):
            b = r.get("bioguide_id") or ""
            if r.get("icpsr"):
                if b in members:
                    icpsr.setdefault(int(float(r["icpsr"])), b)
            c = int(r["congress"])
            if c >= FIRST_CONGRESS and b in members and r.get("nominate_dim1"):
                nom[(b, c)] = float(r["nominate_dim1"])
    log(f"DW-NOMINATE scores: {len(nom):,} member-Congresses; ICPSR ids mapped: {len(icpsr):,}")
    return nom, icpsr


# --------------------------------------------------------------------------- contexts

_SMALL_WORDS = {"and", "of", "on", "the", "for", "in", "to", "a", "an", "at", "by", "with"}


def clean_ctx_name(name):
    """One spelling per committee across sources: 'Senate the Budget' and
    'Senate Budget', 'Transportation And Infrastructure' and '... and ...'."""
    name = re.sub(r"\s+", " ", name).strip()
    name = re.sub(r"^(House|Senate|Joint) (the |Committee on (the )?)", lambda m: m.group(1) + " ", name)
    words = name.split(" ")
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if i > 0 and (lw in _SMALL_WORDS or lw in ("congressional", "delegation")) and not w.endswith(":"):
            out.append(lw)
        elif w.isupper() and len(w) > 3:
            out.append(w.capitalize())
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


class Contexts:
    def __init__(self):
        self.meta = {}                                   # key -> {kind, congress, chamber, name}
        self.members = collections.defaultdict(dict)     # key -> {bioguide: role}

    def add(self, key, kind, congress, chamber, name, bioguide, role="member"):
        if key not in self.meta:
            self.meta[key] = {"kind": kind, "congress": congress, "chamber": chamber, "name": clean_ctx_name(name)}
        cur = self.members[key].get(bioguide)
        if cur is None or (cur == "member" and role != "member"):
            self.members[key][bioguide] = role


def _role_from_stewart(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return "member"
    if 11 <= code <= 16:
        return "chair"
    if 21 <= code <= 24:
        return "ranking"
    if code >= 31:
        return "leader"
    return "member"


def _title_name(s):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s.title() if s.isupper() else s


def load_stewart(ctx, icpsr_map):
    import pandas as pd
    n = miss = 0
    for chamber, fn, code_col, nm_col in (("H", "house_assignments_103-115-3.xls", "Committee code", "Committee Name"),
                                          ("S", "senate_assignments_103-115-3.xls", "Committee Code", "Committee Name")):
        df = pd.read_excel(os.path.join(RAW, fn))
        for r in df.itertuples(index=False):
            row = dict(zip(df.columns, r))
            try:
                c, code, ic = int(row["Congress"]), int(row[code_col]), int(row["ID #"])
            except (TypeError, ValueError):
                continue
            name = _title_name(row.get(nm_col) or "")
            if not name or name.lower().startswith("no committee"):
                continue
            b = icpsr_map.get(ic)
            if not b:
                miss += 1
                continue
            role = _role_from_stewart(row.get("Senior Party Member"))
            if code in (661, 662):
                ctx.add(f"lead-{chamber}-{c}", "leadership", c, chamber,
                        "House leadership" if chamber == "H" else "Senate leadership", b, "leader")
            else:
                kind = "committee"
                if "(Joint" in name or "(JOINT" in name.upper():
                    chamber_k = "J"
                else:
                    chamber_k = chamber
                ctx.add(f"stw-{chamber_k}-{code}-{c}", kind, c, chamber_k, _committee_label(chamber_k, name), b, role)
            n += 1
    log(f"Stewart & Woon assignments: {n:,} rows; {miss:,} rows with an unknown ICPSR id")


def _committee_label(chamber, name):
    name = re.sub(r"\s*\((Select|Joint)\)\s*$", lambda m: f" ({m.group(1)})", name)
    if chamber == "J" or name.lower().startswith(("joint ", "house ", "senate ")):
        return name
    return ("House " if chamber == "H" else "Senate ") + name


def load_committee_names():
    import yaml
    names = {}
    for fn in ("committees-historical.yaml", "committees-current.yaml"):
        with open(os.path.join(RAW, fn), encoding="utf-8") as f:
            for c in yaml.safe_load(f):
                tid = c.get("thomas_id")
                if not tid:
                    continue
                names[tid] = c.get("name", tid)
                for s in c.get("subcommittees") or []:
                    names[tid + s["thomas_id"]] = f'{c.get("name", tid)}: {s.get("name", "")}'.strip()
    return names


def load_snapshots(ctx, members):
    """committee-membership-current.yaml at every commit since 2012: full
    committees from the 116th Congress on (Stewart & Woon covers earlier),
    subcommittees from the 113th."""
    import yaml
    names = load_committee_names()
    files = sorted(glob.glob(os.path.join(RAW, "membership_snapshots", "*.yaml")))
    n = 0
    for fp in files:
        c = congress_of(_d(os.path.basename(fp)[:10]))
        if c < 113:
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            continue
        for tid, rows in data.items():
            sub = len(tid) > 4
            if not sub and c < 116:
                continue
            chamber = {"H": "H", "S": "S", "J": "J"}.get(tid[0], "J")
            label = names.get(tid) or (f"{names[tid[:4]]}: a subcommittee" if tid[:4] in names else tid)
            if not sub:
                label = _committee_label(chamber, re.sub(r"^(House|Senate|Joint) (Committee on )?", "", label).strip()
                                         if chamber != "J" else label)
            else:
                label = ("House " if chamber == "H" else "Senate " if chamber == "S" else "") + \
                        re.sub(r"^(House|Senate) Committee on ", "", label)
            for r in rows or []:
                b = r.get("bioguide")
                if b not in members:
                    continue
                t = (r.get("title") or "").lower()
                role = "chair" if ("chair" in t and "vice" not in t) else "ranking" if "ranking" in t else "member"
                ctx.add(f"snap-{tid}-{c}", "subcommittee" if sub else "committee", c, chamber, label, b, role)
                n += 1
    log(f"membership snapshots: {len(files)} files, {n:,} membership rows (after per-Congress union)")


def add_derived_contexts(ctx, members):
    """State delegation per Congress (both chambers) and entering class per chamber."""
    first = {}
    for b, m in members.items():
        for c, ch, st, dist, party in m["terms"]:
            if c >= FIRST_CONGRESS and st:
                ctx.add(f"deleg-{st}-{c}", "delegation", c, "X", f"{st} congressional delegation", b)
            k = (b, ch)
            first[k] = min(first.get(k, c), c)
    for (b, ch), c in first.items():
        y = congress_years(c)[0] - 2
        label = f"{'House' if ch == 'H' else 'Senate'} class elected {y} ({c}th Congress)"
        ctx.add(f"class-{ch}-{c}", "class", c, ch, label, b)


# --------------------------------------------------------------------------- co-sponsorship

def _bill_members_billstatus():
    """(congress, sponsor, [cosponsors]) per bill from GovInfo BILLSTATUS zips (108th on)."""
    from lxml import etree
    for zp in sorted(glob.glob(os.path.join(RAW, "billstatus", "BILLSTATUS-*.zip"))):
        try:
            z = zipfile.ZipFile(zp)
        except zipfile.BadZipFile:
            log(f"  bad zip {os.path.basename(zp)}")
            continue
        for nm in z.namelist():
            if not nm.endswith(".xml"):
                continue
            try:
                root = etree.fromstring(z.read(nm))
            except etree.XMLSyntaxError:
                continue
            bill = root.find("bill")
            if bill is None:
                continue
            c = bill.findtext("congress")
            sp = [x.text for x in bill.findall("sponsors/item/bioguideId") if x.text]
            co = []
            for it in bill.findall("cosponsors/item"):
                if (it.findtext("sponsorshipWithdrawnDate") or "").strip():
                    continue
                b = it.findtext("bioguideId")
                if b:
                    co.append(b)
            if sp and c:
                yield int(c), sp[0], co


def _fowler_label_map(members):
    """Fowler/Cornell node label 'Last, First [ST-9]' -> bioguide."""
    by_key = collections.defaultdict(list)
    for b, m in members.items():
        for c, ch, st, dist, party in m["terms"]:
            if c <= 108:
                by_key[(m["last"].lower(), st, ch)].append(b)
    out = {}
    fp = os.path.join(RAW, "fowler", "congress-bills-full", "congress-bills-full-node-labels.txt")
    for line in open(fp, encoding="utf-8", errors="replace"):
        nid, label = line.rstrip("\n").split("\t", 1)
        m = re.match(r"^(.*?),\s*(.*?)\s*\[([A-Z]{2})(?:-(\d+))?\]", label)
        if not m:
            continue
        last, first, st, dist = m.group(1).strip().lower(), m.group(2).lower(), m.group(3), m.group(4)
        ch = "H" if dist is not None else "S"
        cands = sorted(set(by_key.get((last, st, ch), [])))
        if len(cands) > 1:
            f1 = first.split()[0] if first else ""
            cands = [b for b in cands if members[b]["first"].lower().startswith(f1[:3])
                     or members[b]["nick"].lower().startswith(f1[:3])] or cands
        if len(cands) == 1:
            out[int(nid)] = cands[0]
    return out


def _bill_members_fowler(members):
    """Bills introduced 1993-01-03 .. 2003-01-02 (BILLSTATUS covers later)."""
    base = os.path.join(RAW, "fowler", "congress-bills-full", "congress-bills-full-")
    lab = _fowler_label_map(members)
    nverts = [int(x) for x in open(base + "nverts.txt")]
    simp = [int(x) for x in open(base + "simplices.txt")]
    times = [int(x) for x in open(base + "times.txt")]
    lo, hi = date(1993, 1, 3), date(2003, 1, 3)
    i = 0
    n_bad_date = 0
    for nv, t in zip(nverts, times):
        ids = simp[i:i + nv]
        i += nv
        try:
            d = date.fromordinal(t)      # Python ordinal: the busiest days are Congress opening days (1973-01-03)
        except ValueError:
            n_bad_date += 1
            continue
        if not (lo <= d < hi):
            continue
        bs = [lab[x] for x in ids if x in lab]
        if bs:
            # Cornell's simplices don't mark the sponsor; treat the first as sponsor
            yield congress_of(d), bs[0], bs[1:]
    log(f"  Fowler labels mapped to members: {len(lab):,}; unreadable dates: {n_bad_date}")


def load_cosponsorship(members):
    small = collections.Counter()        # (a, b) -> bills with <= SMALL_BILL participants shared
    lead = collections.Counter()         # (a, b) -> bills where one sponsored, the other cosponsored
    last_c = {}
    n_bills = collections.Counter()
    for source, gen in (("fowler", _bill_members_fowler(members)), ("billstatus", _bill_members_billstatus())):
        for c, sp, co in gen:
            if c < FIRST_CONGRESS:
                continue
            people = [sp] + [x for x in dict.fromkeys(co) if x != sp]
            people = [p for p in people if p in members]
            if len(people) < 2:
                continue
            n_bills[source] += 1
            if len(people) <= LEAD_BILL_MAX:
                for x in people[1:]:
                    k = (sp, x) if sp < x else (x, sp)
                    lead[k] += 1
                    last_c[k] = max(last_c.get(k, 0), c)
            if len(people) <= SMALL_BILL:
                for i in range(len(people)):
                    for j in range(i + 1, len(people)):
                        a, b = people[i], people[j]
                        k = (a, b) if a < b else (b, a)
                        small[k] += 1
                        last_c[k] = max(last_c.get(k, 0), c)
    log(f"co-sponsorship: bills used {dict(n_bills)}; pairs on small bills {len(small):,}, "
        f"sponsor-cosponsor pairs {len(lead):,}")
    return small, lead


# --------------------------------------------------------------------------- caucuses

# Caucuses whose Wikipedia member list outlived the caucus: the Tea Party
# Caucus went dormant after the 113th Congress but its article kept listing
# members (seen in the 2026-11-18 scrape).
CAUCUS_ACTIVE = {"Tea Party Caucus": (112, 113)}


def load_caucuses(ctx, members):
    """data/raw/congress/caucuses.jsonl, written by congress_caucuses.py:
    {"caucus": name, "bioguide": id, "congress": n}."""
    fp = os.path.join(RAW, "caucuses.jsonl")
    if not os.path.exists(fp):
        log("caucuses: no caucuses.jsonl yet (congress_caucuses.py), skipped")
        return
    n = 0
    for line in open(fp, encoding="utf-8"):
        r = json.loads(line)
        lo, hi = CAUCUS_ACTIVE.get(r["caucus"], (0, 999))
        if r["bioguide"] in members and r["congress"] >= FIRST_CONGRESS and lo <= r["congress"] <= hi:
            slug = re.sub(r"[^a-z0-9]+", "-", r["caucus"].lower())
            ctx.add(f"caucus-{slug}-{r['congress']}", "caucus", r["congress"], "H", r["caucus"], r["bioguide"])
            n += 1
    log(f"caucuses: {n:,} member-Congress memberships")


# --------------------------------------------------------------------------- build all

def build():
    members = load_members()
    nom, icpsr = load_voteview(members)
    ctx = Contexts()
    load_stewart(ctx, icpsr)
    load_snapshots(ctx, members)
    add_derived_contexts(ctx, members)
    load_caucuses(ctx, members)
    small, lead = load_cosponsorship(members)
    kinds = collections.Counter(m["kind"] for m in ctx.meta.values())
    log(f"contexts: {dict(kinds)}")
    model = {"members": members, "nom": nom, "ctx_meta": ctx.meta, "ctx_members": dict(ctx.members),
             "cosp_small": small, "cosp_lead": lead}
    with gzip.open(CACHE, "wb") as f:
        pickle.dump(model, f)
    log(f"saved {CACHE}")
    return model


def load_model():
    with gzip.open(CACHE, "rb") as f:
        return pickle.load(f)


# --------------------------------------------------------------------------- matching to graph nodes

def _norm(s):
    s = unicodedata_normalize(s).lower()
    s = re.sub(r"\([^)]*\)\s*$", "", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def unicodedata_normalize(s):
    import unicodedata
    return "".join(ch for ch in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(ch))


def _nick_of():
    sys.path.insert(0, os.path.join(HERE, "webapp"))
    from nicknames import NICKNAMES
    rev = collections.defaultdict(set)
    for short, formals in NICKNAMES.items():
        for f in formals:
            rev[f].add(short)
    return rev


_NICK_OF = _nick_of()


def name_variants(m):
    first, mid, last, nick, suf = (m["first"], m["middle"], m["last"], m["nick"], m["suffix"])
    out = {m["name"], f"{first} {last}"}
    if nick:
        out.add(f"{nick} {last}")
    if mid:
        out |= {f"{first} {mid} {last}", f"{first} {mid[0]} {last}", f"{mid} {last}"}   # Thomas Slade Gorton -> Slade Gorton
    for short in _NICK_OF.get(_norm(first), ()):                          # Samuel Nunn -> Sam Nunn
        out.add(f"{short} {last}")
    if suf:
        out |= {f"{v} {suf}" for v in list(out)}
    return {_norm(v) for v in out if v.strip()}


_OFFICE_WORDS = re.compile(r"congress|senate|house|campaign|committee|pac\b|victory|friends of|for america|"
                           r"leadership fund|for us|for (?:ohio|texas|[a-z]+)\b", re.I)
_CHAMBER_NODES = {"united states house of representatives", "united states senate", "united states congress",
                  "us house of representatives", "u s house of representatives", "u s senate"}
_OFFICE_CATS = {"PUBLIC_OFFICE", "FELLOW_OFFICEHOLDER", "STAFF_AIDE"}


def match_members(members):
    """bioguide -> (graph label or None, method). Method: 'office' (name match
    with office/campaign evidence on the node), 'name' (one node with the name,
    no evidence), 'new' (not in the graph: a node is created by the build)."""
    log("loading graph for matching ...")
    g = json.load(gzip.open(GRAPH, "rt", encoding="utf-8"))
    nodes = g["nodes"]
    wd = wikidata_names(members)
    variants = {b: name_variants(m) | {_norm(n) for n in wd.get(m.get("qid") or "", []) if len(n.split()) >= 2}
                for b, m in members.items()}
    want = collections.defaultdict(set)
    for b, vs in variants.items():
        for v in vs:
            want[v].add(b)
    cand = collections.defaultdict(list)            # bioguide -> [node idx]
    for i, lab in enumerate(nodes):
        k = _norm(lab)
        if k in want:
            for b in want[k]:
                cand[b].append(i)
    cidx = {i for v in cand.values() for i in v}
    nbr = collections.defaultdict(list)
    deg = collections.Counter()
    for s, t, p, cats in g["edges"]:
        if s in cidx:
            nbr[s].append((t, cats))
            deg[s] += 1
        if t in cidx:
            nbr[t].append((s, cats))
            deg[t] += 1
    out, methods = {}, collections.Counter()
    for b, m in members.items():
        last = _norm(m["last"])
        best = None
        for i in cand.get(b, []):
            ev = 0
            for o, cats in nbr[i]:
                ol = nodes[o].lower()
                if _norm(nodes[o]) in _CHAMBER_NODES or set(cats) & _OFFICE_CATS:
                    ev += 1
                elif last and last in _norm(nodes[o]) and _OFFICE_WORDS.search(ol):
                    ev += 1
            key = (ev, "(" not in nodes[i], deg[i])
            if best is None or key > best[0]:
                best = (key, i)
        if best and best[0][0] > 0:
            out[b] = (nodes[best[1]], "office")
        elif best and len(cand[b]) == 1:
            out[b] = (nodes[best[1]], "name")
        else:
            out[b] = (None, "new")
        methods[out[b][1]] += 1
    log(f"member -> node matches: {dict(methods)}")
    return out, nodes


WD_NAMES = os.path.join(RAW, "wikidata_names.json")


def wikidata_names(members):
    """QID -> [English label + aliases], cached in data/raw/congress."""
    if os.path.exists(WD_NAMES):
        return json.load(open(WD_NAMES, encoding="utf-8"))
    import requests
    qids = sorted({m["qid"] for m in members.values() if m.get("qid")})
    out = collections.defaultdict(list)
    for i in range(0, len(qids), 200):
        vals = " ".join(f"wd:{q}" for q in qids[i:i + 200])
        q = f"""SELECT ?p ?n WHERE {{ VALUES ?p {{ {vals} }}
                 {{ ?p rdfs:label ?n }} UNION {{ ?p skos:altLabel ?n }} FILTER(LANG(?n) = "en") }}"""
        r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                         headers={"User-Agent": "sixdegrees-congress-pilot/1.0"}, timeout=120)
        r.raise_for_status()
        for b in r.json()["results"]["bindings"]:
            out[b["p"]["value"].rsplit("/", 1)[1]].append(b["n"]["value"])
    json.dump(out, open(WD_NAMES, "w", encoding="utf-8"))
    log(f"Wikidata names fetched for {len(out):,} members")
    return out


def new_node_label(m):
    last_term = max(m["terms"])
    office = "U.S. Representative" if last_term[1] == "H" else "U.S. Senator"
    return f"{m['name']} ({office}, {last_term[2]})"


# --------------------------------------------------------------------------- features

KINDS = ["subcommittee", "committee", "leadership", "caucus", "class", "delegation"]
# sub_data / caucus_data (share of the overlap after subcommittee / caucus data
# begins) were dropped 2026-11-18: they only measured "served recently", which
# the news-derived labels favour, and became the strongest feature.
# Years together by chamber, not one "same chamber" count: the Senate is 100
# people who all work together, the House 435 (a single count came out
# strongly negative, 2026-11-18).
# hh / ss / xch (years together per chamber) and cls (entering class) came out
# with the wrong sign once negatives were sampled uniformly (2026-11-18) and
# are left out, per the spec's rule; repeated service still counts through the
# per-Congress committee, subcommittee and delegation contexts.
# Committees and subcommittees as one "committee work" sum (cmte): fitted
# separately, committees came out slightly negative next to subcommittees,
# which only exist from 2013 (2026-11-18).
FEATURES = ["cmte", "lead", "caucus", "deleg", "chair_rank",
            "same_party", "nom_dist", "cosp_small", "cosp_lead", "xparty_work"]
# xparty_work: co-sponsorship and chair/ranking pairs across party lines
# (McCain-Feingold, Dodd-Shelby). Without it a linear model applies the
# cross-party discount to bipartisan partners too (2026-11-18).
_KIND_F = {"subcommittee": "sub", "committee": "com", "leadership": "lead", "caucus": "caucus",
           "class": "cls", "delegation": "deleg"}


def pair_contexts(model):
    """(a, b) -> {feature sums, 'ctx': [context keys]} for every pair sharing a context."""
    pairs = {}
    meta, cm = model["ctx_meta"], model["ctx_members"]
    for key, mem in cm.items():
        kind = meta[key]["kind"]
        size = len(mem)
        if size < 2:
            continue
        w = 1.0 / math.log2(size + 1)
        f = _KIND_F[kind]
        bs = sorted(mem)
        for i in range(len(bs)):
            for j in range(i + 1, len(bs)):
                k = (bs[i], bs[j])
                d = pairs.get(k)
                if d is None:
                    d = pairs[k] = {"ctx": []}
                d[f] = d.get(f, 0.0) + w
                d["ctx"].append(key)
                if {mem[bs[i]], mem[bs[j]]} == {"chair", "ranking"}:
                    d["chair_rank"] = d.get("chair_rank", 0) + 1
    return pairs


def served_index(members):
    sv = collections.defaultdict(dict)
    for b, m in members.items():
        for c, ch, st, dist, party in m["terms"]:
            if c >= FIRST_CONGRESS:
                sv[b][c] = (ch, party, st)
    return sv


def features(a, b, d, sv, nom, small, lead):
    x = {f: 0.0 for f in FEATURES + ["sub", "com", "cls"]}
    for f in ("sub", "com", "lead", "caucus", "cls", "deleg", "chair_rank"):
        x[f] = float(d.get(f, 0.0)) if d else 0.0
    x["cmte"] = x["sub"] + x["com"]
    both = set(sv[a]) & set(sv[b])
    x["overlap"] = len(both)
    x["hh"] = sum(sv[a][c][0] == sv[b][c][0] == "H" for c in both)
    x["ss"] = sum(sv[a][c][0] == sv[b][c][0] == "S" for c in both)
    x["xch"] = sum(sv[a][c][0] != sv[b][c][0] for c in both)
    x["same_party"] = (sum(sv[a][c][1] == sv[b][c][1] for c in both) / len(both)) if both else 0.0
    ds = [abs(nom[(a, c)] - nom[(b, c)]) for c in both if (a, c) in nom and (b, c) in nom]
    x["nom_dist"] = sum(ds) / len(ds) if ds else 0.5
    k = (a, b) if a < b else (b, a)
    x["cosp_small"] = math.log1p(small.get(k, 0))
    x["cosp_lead"] = math.log1p(lead.get(k, 0))
    x["xparty_work"] = (1.0 - x["same_party"]) * (x["cosp_small"] + x["chair_rank"])
    x["sub_data"] = (sum(c >= 113 for c in both) / len(both)) if both else 0.0
    x["caucus_data"] = (sum(c >= 109 for c in both) / len(both)) if both else 0.0
    return x


def positive_labels(members, matches):
    """Pairs with independent evidence of a real tie, from the pipeline DB."""
    import psycopg2
    import pg_secret
    by_name = collections.defaultdict(set)
    for b, m in members.items():
        lab = matches.get(b, (None,))[0]
        for v in name_variants(m) | ({_norm(lab)} if lab else set()):
            by_name[v].add(b)
    uniq = {v: next(iter(bs)) for v, bs in by_name.items() if len(bs) == 1}
    conn = psycopg2.connect(pg_secret.load_secret("DATABASE_URL"), connect_timeout=30)
    cur = conn.cursor()
    cur.execute("""SELECT source_name, target_name, source_data, relation_type FROM relationships
                   WHERE (source_data = 'LITTLESIS' AND relation_type IN ('SOCIAL', 'PROFESSIONAL', 'FAMILY'))
                      OR (source_data = 'GDELT_HAIKU' AND relation_type <> 'CO_STAR')""")
    pos = collections.Counter()
    src = collections.Counter()
    for s, t, sd, rt in cur:
        a, b = uniq.get(_norm(s)), uniq.get(_norm(t))
        if a and b and a != b:
            k = (a, b) if a < b else (b, a)
            pos[k] += 1
            src[f"{sd}:{rt}"] += 1
    conn.close()
    log(f"positive pairs: {len(pos):,} from {dict(src.most_common(12))}")
    return set(pos)


def fit(model, matches, rng_seed=7):
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.preprocessing import StandardScaler
    members, nom = model["members"], model["nom"]
    small, lead = model["cosp_small"], model["cosp_lead"]
    sv = served_index(members)
    log("pair contexts ...")
    pc = pair_contexts(model)
    for k in set(small) | set(lead):
        pc.setdefault(k, {"ctx": []})
    log(f"candidate pairs (shared a context or a bill): {len(pc):,}")
    pos = positive_labels(members, matches)
    # negatives: random pairs serving in the same Congress (either chamber,
    # like the positives), 20 per positive
    rng = random.Random(rng_seed)
    by_cc = collections.defaultdict(list)
    for b, cs in sv.items():
        for c in cs:
            by_cc[c].append(b)
    cc_keys = list(by_cc)
    neg = set()
    while len(neg) < 20 * max(len(pos), 1):
        c = rng.choice(cc_keys)
        a, b = rng.sample(by_cc[c], 2)
        k = (a, b) if a < b else (b, a)
        # a pair sharing n Congresses is drawn n times as often; keep it with
        # probability 1/n so negatives are uniform over pairs, like positives
        if k not in pos and rng.random() < 1.0 / len(set(sv[a]) & set(sv[b])):
            neg.add(k)
    rows, ys, first = [], [], []
    for k, y in [(k, 1) for k in pos] + [(k, 0) for k in neg]:
        x = features(k[0], k[1], pc.get(k), sv, nom, small, lead)
        both = set(sv[k[0]]) & set(sv[k[1]])
        if not both:
            continue                       # never served together after 1993: outside the model
        rows.append([x[f] for f in FEATURES])
        ys.append(y)
        first.append(min(both))
    X, y, first = np.array(rows), np.array(ys), np.array(first)
    train, test = first <= 113, first > 113
    log(f"labelled pairs: {len(y):,} ({int(y.sum()):,} positive); train {int(train.sum()):,}, test {int(test.sum()):,}")
    sc = StandardScaler().fit(X[train])
    lr = LogisticRegression(max_iter=2000, C=1.0).fit(sc.transform(X[train]), y[train])
    auc = roc_auc_score(y[test], lr.predict_proba(sc.transform(X[test]))[:, 1])
    # refit on everything for scoring
    sc_all = StandardScaler().fit(X)
    lr_all = LogisticRegression(max_iter=2000, C=1.0).fit(sc_all.transform(X), y)
    p_test = lr.predict_proba(sc.transform(X[test]))[:, 1]
    report = [f"model {MODEL_VERSION}", f"test AUC (pairs who first served together after 2014): {auc:.3f}",
              "coefficients (standardised, full fit):"]
    for f, w in sorted(zip(FEATURES, lr_all.coef_[0]), key=lambda t: -abs(t[1])):
        report.append(f"  {f:12s} {w:+.3f}")
    report.append("test precision by score threshold (share of pairs above it with independent evidence):")
    for t in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        sel = p_test >= t
        if sel.sum():
            report.append(f"  p >= {t:.1f}: {int(sel.sum()):5d} pairs, precision {y[test][sel].mean():.2f}, "
                          f"recall {y[test][sel].sum() / max(y[test].sum(), 1):.2f}")
    log("\n".join(report))
    return {"scaler": sc_all, "lr": lr_all, "pairs": pc, "sv": sv, "report": report, "auc": auc,
            "pos": pos}


# --------------------------------------------------------------------------- scoring + explanations

_KIND_ORDER = {"leadership": 0, "subcommittee": 1, "committee": 2, "caucus": 3, "class": 4, "delegation": 5}
_PARTY = {"Democrat": "Democrats", "Republican": "Republicans", "Independent": "independents"}


_RAW_CODE = re.compile(r"^(House|Senate|Joint) [A-Za-z]{4}\d{2}$")    # a subcommittee id with no name on file


def explain(a, b, d, sv, small, lead, members, meta, strength, ctx_members=None):
    groups = collections.defaultdict(list)
    roles = collections.defaultdict(list)
    for key in (d or {}).get("ctx", []):
        m = meta[key]
        name = clean_ctx_name(m["name"])
        if _RAW_CODE.match(name):
            continue
        cm = (ctx_members or {}).get(key, {})
        if {cm.get(a), cm.get(b)} == {"chair", "ranking"}:
            roles[name].append(m["congress"])
        else:
            groups[(m["kind"], name)].append(m["congress"])
    items = sorted(groups.items(), key=lambda kv: (_KIND_ORDER[kv[0][0]], -len(kv[1])))
    parts = [f"chair and ranking member of {name} ({years_label(cs)})" for name, cs in roles.items()][:2]
    parts += [f"{name} ({years_label(cs)})" for (kind, name), cs in items[:3 - min(len(parts), 2)]]
    both = set(sv[a]) & set(sv[b])
    parties = {sv[a][c][1] for c in both} | {sv[b][c][1] for c in both}
    if len(parties) == 1 and next(iter(parties)) in _PARTY:
        parts.append(f"both {_PARTY[next(iter(parties))]}")
    k = (a, b) if a < b else (b, a)
    n = max(small.get(k, 0), lead.get(k, 0))
    if n:
        parts.append(f"co-sponsored {n} bill{'s' if n != 1 else ''} together")
    if not parts:
        parts.append(f"served in Congress together ({years_label(both)})")
    text = "; ".join(parts)
    return ("Weak tie: " + text) if strength == "weak" else text


def score_all(model, fitted, matches):
    import numpy as np
    members, nom = model["members"], model["nom"]
    small, lead, meta = model["cosp_small"], model["cosp_lead"], model["ctx_meta"]
    sv, pc = fitted["sv"], fitted["pairs"]
    keys = [k for k in pc if set(sv[k[0]]) & set(sv[k[1]])]
    out = []
    B = 200_000
    for i in range(0, len(keys), B):
        chunk = keys[i:i + B]
        X = np.array([[features(a, b, pc[(a, b)], sv, nom, small, lead)[f] for f in FEATURES] for a, b in chunk])
        ps = fitted["lr"].predict_proba(fitted["scaler"].transform(X))[:, 1]
        for (a, b), p in zip(chunk, ps):
            # chair and ranking member of the same committee negotiate every
            # bill it handles; the news-derived labels rarely record that, so
            # the model alone under-weights it (Dodd-Shelby, Waxman-Barton)
            if pc[(a, b)].get("chair_rank", 0) >= 1:
                p = max(p, STRONG_P)
            if p < WEAK_P:
                continue
            strength = "strong" if p >= STRONG_P else "weak"
            out.append((a, b, float(min(p, P_CAP)), strength,
                        explain(a, b, pc[(a, b)], sv, small, lead, members, meta, strength,
                                model["ctx_members"])))
    log(f"scored pairs kept: {len(out):,} (strong {sum(r[3] == 'strong' for r in out):,}, "
        f"weak {sum(r[3] == 'weak' for r in out):,}) of {len(keys):,} candidates")
    return out


# --------------------------------------------------------------------------- export

DDL = """
CREATE TABLE IF NOT EXISTS cg_member (bioguide text PRIMARY KEY, name text, node_label text, match_method text,
    qid text, icpsr int, first_congress int, last_congress int);
CREATE TABLE IF NOT EXISTS cg_context (context_key text PRIMARY KEY, kind text, congress int, chamber text,
    name text, size int);
CREATE TABLE IF NOT EXISTS cg_membership (context_key text, bioguide text, role text);
CREATE TABLE IF NOT EXISTS cg_tie_candidate (a_bioguide text, b_bioguide text, a_node text, b_node text, p real,
    strength text, explanation text, model_version text);
"""


def export(model, matches, ties, report):
    import psycopg2
    import pg_secret
    from psycopg2.extras import execute_values
    members, meta, cm = model["members"], model["ctx_meta"], model["ctx_members"]
    label = {b: (matches[b][0] or new_node_label(members[b])) for b in members}
    conn = psycopg2.connect(pg_secret.load_secret("DATABASE_URL"), connect_timeout=30)
    cur = conn.cursor()
    cur.execute(DDL)
    cur.execute("TRUNCATE cg_member, cg_context, cg_membership, cg_tie_candidate")
    execute_values(cur, "INSERT INTO cg_member VALUES %s", [
        (b, m["name"], label[b], matches[b][1], m["qid"], m["icpsr"],
         min(c for c, *_ in m["terms"]), max(c for c, *_ in m["terms"])) for b, m in members.items()])
    execute_values(cur, "INSERT INTO cg_context VALUES %s", [
        (k, v["kind"], v["congress"], v["chamber"], v["name"], len(cm.get(k, {}))) for k, v in meta.items()])
    execute_values(cur, "INSERT INTO cg_membership VALUES %s",
                   [(k, b, r) for k, mem in cm.items() for b, r in mem.items()], page_size=5000)
    execute_values(cur, "INSERT INTO cg_tie_candidate VALUES %s",
                   [(a, b, label[a], label[b], p, s, e, MODEL_VERSION) for a, b, p, s, e in ties], page_size=5000)
    conn.commit()
    conn.close()
    log(f"Postgres: cg_member {len(members):,}, cg_context {len(meta):,}, cg_tie_candidate {len(ties):,}")
    with gzip.open(TIES_LOCAL, "wb") as f:
        pickle.dump({"ties": ties, "label": label}, f)
    log(f"local copy for evaluation: {TIES_LOCAL}")


def publish():
    """Make the staged ties live for the next nightly build (after review)."""
    import psycopg2
    import pg_secret
    conn = psycopg2.connect(pg_secret.load_secret("DATABASE_URL"), connect_timeout=30)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS cg_tie")
    cur.execute("CREATE TABLE cg_tie AS SELECT * FROM cg_tie_candidate")
    cur.execute("SELECT count(*), count(*) FILTER (WHERE strength = 'strong') FROM cg_tie")
    n, strong = cur.fetchone()
    conn.commit()
    conn.close()
    log(f"published: cg_tie now has {n:,} pairs ({strong:,} strong); the next graph rebuild uses them")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["load", "match", "fit", "export", "publish", "all"])
    a = ap.parse_args()
    if a.step == "publish":
        return publish()
    model = build() if a.step in ("load", "all") else load_model()
    if a.step == "load":
        return
    matches, _ = match_members(model["members"])
    if a.step == "match":
        bad = [(b, model["members"][b]["name"]) for b, (lab, how) in matches.items() if how != "office"]
        log(f"not matched with office evidence ({len(bad)}), e.g. {bad[:25]}")
        return
    fitted = fit(model, matches)
    if a.step == "fit":
        return
    ties = score_all(model, fitted, matches)
    export(model, matches, ties, fitted["report"])


if __name__ == "__main__":
    main()
