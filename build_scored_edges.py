"""Rebuild a SCORED edge list from the source DB, preserving multiple relations
per pair, re-applying working-v2 cleanups, and computing noisy-OR pair scores.

Output: webapp/data/graph_scored.json.gz
  {nodes: [...], edges: [[u, v, prob, [used_categories]], ...]}

Pipeline:
  1. Read all relationships from pipeline_cache.db
  2. Apply working-v2 cleanups:
     - drop FELLOW_REPRESENTATIVE / FELLOW_SENATOR (runaway cliques)
     - drop OWNERSHIP entirely (corrupt person-as-org)
     - drop conflated person-as-org position edges
     - drop SAME_ENTITY (alias) from scoring
  3. Group all relation types per undirected pair
  4. Categorize -> dedup -> noisy-OR score
  5. Emit scored edge list
"""
import sqlite3, json, gzip, os, re, sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))
from relation_categories import categorize
from link_scoring import score_pair

DB = "data/pipeline_cache.db"
OUT = "webapp/data/graph_scored.json.gz"

# --- cleanup helpers (from working-v2) ---
DROP_RELATIONS = {"FELLOW_REPRESENTATIVE", "FELLOW_SENATOR", "OWNERSHIP"}
POS_RELS = {
    "OWNERSHIP", "POSITION", "DIRECTOR", "CEO", "CHAIRMAN", "PRESIDENT",
    "BOARD_MEMBER", "BOARD_MEMBER_OF", "TRUSTEE", "OFFICER", "CFO", "COO",
    "PARTNER", "FOUNDER", "MEMBER", "MEMBERSHIP", "EXECUTIVE_VICE_PRESIDENT",
    "CHIEF_OF_STAFF", "SENIOR_VICE_PRESIDENT", "VICE_PRESIDENT", "GENERAL_COUNSEL",
    "MANAGING_DIRECTOR", "CHIEF_EXECUTIVE_OFFICER", "CHIEF_FINANCIAL_OFFICER",
    "INDEPENDENT_DIRECTOR", "CHAIR", "VICE_CHAIRMAN", "EXECUTIVE_CHAIRMAN",
    "LOBBYING", "LOBBYIST", "EMPLOYER",
}
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

# Build person-name set for conflation detection
conn = sqlite3.connect(DB)
c = conn.cursor()
persons = set()
c.execute("SELECT DISTINCT source_name FROM relationships WHERE source_type='PERSON'")
persons |= {r[0].lower() for r in c.fetchall() if r[0]}
c.execute("SELECT DISTINCT target_name FROM relationships WHERE target_type='PERSON'")
persons |= {r[0].lower() for r in c.fetchall() if r[0]}
print(f"Person names: {len(persons)}")

# Gather relations per undirected pair (preserving ALL types)
pair_rels = defaultdict(set)        # (name_a, name_b) -> set of raw relation strings
name_canon = {}                     # lowercase -> display name
c.execute("SELECT source_name, target_name, relation_type FROM relationships")
n_raw = n_drop = 0
for s, t, r in c.fetchall():
    n_raw += 1
    if not s or not t or s == t:
        continue
    if r in DROP_RELATIONS:
        n_drop += 1
        continue
    sl, tl = s.lower(), t.lower()
    # conflation: position-type edge between two people -> drop
    if r in POS_RELS:
        s_person = sl in persons or looks_like_person(s)
        t_person = tl in persons or looks_like_person(t)
        if s_person and t_person:
            n_drop += 1
            continue
    key = tuple(sorted([sl, tl]))
    pair_rels[key].add(r)
    name_canon.setdefault(sl, s)
    name_canon.setdefault(tl, t)
conn.close()
print(f"Raw relationship rows: {n_raw}, dropped by cleanup: {n_drop}")
print(f"Unique scorable pairs: {len(pair_rels)}")

# --- Merge Wikidata time-overlap edges (if harvested) ---
# Only connect people who ALREADY exist as graph nodes, so we densify the
# existing network rather than appending disconnected Wikidata names.
OVERLAP_FILE = "wikidata_overlap_edges.jsonl"
if os.path.exists(OVERLAP_FILE):
    existing = set(name_canon.keys())  # lowercase names already in the graph
    n_ov = n_ov_kept = 0
    with open(OVERLAP_FILE, "r", encoding="utf-8") as f:
        for line in f:
            n_ov += 1
            try:
                e = json.loads(line)
            except ValueError:
                continue
            al, bl = e["a"].lower(), e["b"].lower()
            # require BOTH endpoints to already be graph nodes
            if al in existing and bl in existing and al != bl:
                key = tuple(sorted([al, bl]))
                pair_rels[key].add(e["rel"])
                n_ov_kept += 1
    print(f"Wikidata overlap edges: {n_ov} read, {n_ov_kept} connect existing nodes")
    print(f"Unique scorable pairs after overlap merge: {len(pair_rels)}")

# Score each pair
node_ids = {}
nodes = []
def nid(name_lower):
    if name_lower not in node_ids:
        node_ids[name_lower] = len(nodes)
        nodes.append(name_canon.get(name_lower, name_lower))
    return node_ids[name_lower]

edges = []
multi = 0
for (a, b), rels in pair_rels.items():
    cats = [categorize(r) for r in rels]
    prob, used = score_pair(cats)
    if prob is None:
        continue  # only SAME_ENTITY / non-relations
    if len(set(cats)) > 1:
        multi += 1
    edges.append([nid(a), nid(b), round(prob, 4), used])

print(f"Scored edges: {len(edges)}  (pairs with multiple relation types: {multi})")

with gzip.open(OUT, "wt", encoding="utf-8") as f:
    json.dump({"nodes": nodes, "edges": edges}, f, separators=(",", ":"))
print(f"Saved {OUT}: {os.path.getsize(OUT)/1024/1024:.1f} MB, {len(nodes)} nodes")
