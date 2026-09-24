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
import json, gzip, os, re, sys, unicodedata
from collections import defaultdict, Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))
from relation_categories import categorize
from link_scoring import score_pair


# --- name canonicalization (2026-09-10) -----------------------------------
# The graph fragments the same person across spelling variants: "Meghan
# O'Sullivan" / "Meghan OSullivan" were two separate nodes, her board/donation
# edges attaching to one and stray edges to the other. Root cause: endpoints
# were matched by bare name.lower() only. This collapses variants that are
# identical after removing PUNCTUATION and DIACRITICS ONLY -- apostrophes,
# periods, hyphens, commas, parenthetical nicknames, accents.
#
# Deliberately does NOT touch:
#   * generational suffixes (Jr / Sr / II / III / IV) -- these DISAMBIGUATE.
#     A measured test merge that stripped them fused five John Jacob Astors
#     (incl. the Titanic one) and President Benjamin Harrison with the
#     Declaration signer into single nodes. Suffixes stay.
#   * middle initials ("John A Smith" vs "John Smith") -- real false-merge
#     risk without structural corroboration; left for a later, gated pass.
# So this is a small (~6K nodes, ~1%), zero-judgement-call merge.
_COMBINING = dict.fromkeys(range(0x300, 0x370))  # combining diacritical marks block
_CK_KEEP = re.compile(r"[^\w ]|_")
_CK_WS = re.compile(r"\s+")
_ck_cache = {}  # ~8M distinct names but ~150M+ rows -> memoize hard


def canon_key(name):
    """Merge key: lowercase, strip diacritics to base latin, drop everything
    that isn't a letter/digit/space. Non-latin scripts are preserved (\\w is
    unicode-aware) so they key on themselves rather than collapsing to a
    degenerate empty string. If the strip leaves <2 chars (pure punctuation),
    fall back to the raw lowercased form so junk like "-" can't become a
    merge magnet."""
    hit = _ck_cache.get(name)
    if hit is not None:
        return hit
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = _CK_WS.sub(" ", _CK_KEEP.sub("", s)).strip()
    out = s if len(s) >= 2 else name.lower().strip()
    _ck_cache[name] = out
    return out

# NOTE (2026-09-16): was sqlite3.connect() against a local pipeline_cache.db
# path, with a long comment here about WAL/checkpoint races being the "actual
# root cause" of recurring "database disk image is malformed" failures
# (2026-08-29, 2026-08-31, then again 2026-09-14/15/16 -- three days straight,
# confirmed via the task log, which is what surfaced this in the first
# place). That diagnosis was already superseded once: the whole project
# migrated off SQLite to Postgres specifically because of this exact failure
# class (see [[project_postgres_migration]], [[project_db_corruption_2026_09_11]])
# -- this script was simply missed in that migration, so it kept reading a
# local SQLite file nothing has actually written through since, which the
# WAL-checkpoint mitigation could never fully fix (narrows the race, can't
# close it, per the comment it replaced). Moved to Postgres like everything
# else rather than patching the SQLite path again.
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set -- this script is Postgres-only")
OUT = os.environ.get("SCORED_OUT", "webapp/data/graph_scored.json.gz")

# --- cleanup helpers (from working-v2) ---
# MENTIONED_WITH (GDELT news co-mention, scored NEWS_COMENTION=0.10) dropped
# 2026-08-24: 72.37M of 86.1M raw relationship rows (84%) are this one type,
# and GDELT's NER extraction is noisy enough to fabricate PERSON entities out
# of boilerplate/generic text (e.g. "whatsapp linkedin" from share buttons,
# "our lady" from truncated religious/institution names) -- these become
# fake mega-hub nodes (40k+ degree) that dominate k-shortest-paths cost far
# out of proportion to the 0.10 probability weight they're scored at.
DROP_RELATIONS = {"FELLOW_REPRESENTATIVE", "FELLOW_SENATOR", "OWNERSHIP", "MENTIONED_WITH"}
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

import psycopg2

# Build person-name set for conflation detection. A plain (client-side
# buffering) cursor is fine here -- bounded by the number of DISTINCT PERSON
# names (~millions), not the ~165M raw row count -- Postgres uses the
# existing idx_relationships_source_type/idx_relationships_target_type
# indexes (confirmed via EXPLAIN; no SQLite-style INDEXED BY hint needed or
# supported).
conn = psycopg2.connect(DATABASE_URL)
c = conn.cursor()
persons = set()
c.execute("SELECT DISTINCT source_name FROM relationships WHERE source_type='PERSON'")
persons |= {r[0].lower() for r in c.fetchall() if r[0]}
c.execute("SELECT DISTINCT target_name FROM relationships WHERE target_type='PERSON'")
persons |= {r[0].lower() for r in c.fetchall() if r[0]}
print(f"Person names: {len(persons)}")

# Gather relations per undirected pair (preserving ALL types).
# Pair keys are canon_key()'d so punctuation/diacritic variants of one person
# land on ONE node. display_votes[key] tallies the raw display forms seen for
# that key so the node label can be the most common (and, tie-broken, the
# richest -- most non-alnum chars, i.e. the one that kept its apostrophes).
pair_rels = defaultdict(set)        # (key_a, key_b) -> set of raw relation strings
display_votes = defaultdict(Counter)  # canon_key -> Counter(raw display form -> count)
def _stream(cur, n=250_000):
    """Batched fetch -- a plain .fetchall() here materialises ~35 GB of Python
    string tuples for the ~165M-row table and thrashes on a loaded machine."""
    while True:
        b = cur.fetchmany(n)
        if not b:
            return
        yield from b

# Named (server-side) cursor: an ordinary psycopg2 cursor buffers the ENTIRE
# result set into client memory as soon as execute() runs -- the exact ~35GB
# problem the batched _stream() helper above was written to avoid, just
# moved from "after the fetch" to "during execute()", and now also paying to
# transfer all of it over the network first. A named cursor is a real
# server-side cursor: itersize controls how many rows come over per
# round-trip, so _stream()'s fetchmany() loop streams from the server
# exactly like it did against SQLite.
stream_cur = conn.cursor(name="build_scored_edges_stream")
stream_cur.itersize = 250_000
stream_cur.execute("SELECT source_name, target_name, relation_type FROM relationships")
c = stream_cur
n_raw = n_drop = n_selfmerge = 0
for s, t, r in _stream(c):
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
    sk, tk = canon_key(s), canon_key(t)
    if sk == tk:                    # different raw strings, same person -> not an edge
        n_selfmerge += 1
        display_votes[sk][s] += 1
        display_votes[sk][t] += 1
        continue
    pair_rels[tuple(sorted([sk, tk]))].add(r)
    display_votes[sk][s] += 1
    display_votes[tk][t] += 1
conn.close()
print(f"Raw relationship rows: {n_raw}, dropped by cleanup: {n_drop}, "
      f"self-loops after canonicalization: {n_selfmerge}")
print(f"Unique scorable pairs: {len(pair_rels)}")

def best_display(key):
    votes = display_votes.get(key)
    if not votes:
        return key
    # most frequent form; tie-break toward the form with the most punctuation
    # (kept its apostrophes/periods) then the longest
    return max(votes, key=lambda d: (votes[d], sum(not ch.isalnum() and not ch.isspace() for ch in d), len(d)))

# --- Merge Wikidata time-overlap edges (if harvested) ---
# Only connect people who ALREADY exist as graph nodes, so we densify the
# existing network rather than appending disconnected Wikidata names.
OVERLAP_FILE = "qid_overlap_edges.jsonl"
if os.path.exists(OVERLAP_FILE):
    existing = set(display_votes.keys())  # canon keys already in the graph
    n_ov = n_ov_kept = 0
    with open(OVERLAP_FILE, "r", encoding="utf-8") as f:
        for line in f:
            n_ov += 1
            try:
                e = json.loads(line)
            except ValueError:
                continue
            al, bl = canon_key(e["a"]), canon_key(e["b"])
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
def nid(key):
    if key not in node_ids:
        node_ids[key] = len(nodes)
        nodes.append(best_display(key))
    return node_ids[key]

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

# aliases: for each node that had >1 raw display form, the alternates (so the
# search index / UI can show "also known as"). Keyed by node index.
aliases = {}
for key, idx in node_ids.items():
    forms = display_votes.get(key)
    if forms and len(forms) > 1:
        chosen = nodes[idx]
        alts = sorted(f for f in forms if f != chosen)
        if alts:
            aliases[idx] = alts
n_merged = sum(len(v) for v in aliases.values())
print(f"Canonicalized: {len(aliases)} nodes carry aliases ({n_merged} variant strings folded in)")

with gzip.open(OUT, "wt", encoding="utf-8") as f:
    json.dump({"nodes": nodes, "edges": edges, "aliases": aliases}, f, separators=(",", ":"))
print(f"Saved {OUT}: {os.path.getsize(OUT)/1024/1024:.1f} MB, {len(nodes)} nodes")
