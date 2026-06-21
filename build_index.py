#!/usr/bin/env python3
"""Build graph pickle + deduplicated name index for the FastAPI pathfinder app.
Filters out location-type nodes (houses, addresses, phone numbers) from search.
"""
import os, sys, pickle, json, re
sys.path.insert(0, os.getcwd())
from collections import defaultdict
from src.config import DB_PATH
from src.data.db_manager import DBManager
from src.graph.graph_compiler import GraphCompiler

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "data")
os.makedirs(OUT, exist_ok=True)

print("Loading graph from DB...")
db = DBManager(DB_PATH)
compiler = GraphCompiler(db)
g = compiler.build_graph()
print(f"Graph: {g.number_of_nodes()} nodes, {g.number_of_edges()} edges")

# Save graph
graph_path = os.path.join(OUT, "graph.pkl")
with open(graph_path, "wb") as f:
    pickle.dump(g, f, protocol=pickle.HIGHEST_PROTOCOL)
print(f"Graph saved to {graph_path}")

# ── Classify node type from name ──
LOCATION_INDICATORS = [
    "house", "residence", "home", "apartment", "mansion", "estate",
    "address", "street", "avenue", "boulevard", "road", "drive", "lane",
    "place", "city", "town", "village", "county", "state", "country",
    "island", "virgin islands", "palm beach", "new york", "london", "paris",
    "zorro trust", "trust #", "insurance trust",
    # Phone numbers
]
PHONE_RE = re.compile(r'^[\d\-\(\)\s]{7,}$')
LOCATION_WORDS = {"house", "residence", "mansion", "apartment", "home"}

def classify_node(name):
    """Classify a node as 'person', 'org', or 'location'."""
    if not name:
        return "unknown"
    name_lower = name.lower().strip()
    # Phone numbers
    if PHONE_RE.match(name_lower):
        return "location"
    # Location indicators in name
    words = set(name_lower.split())
    if words & LOCATION_WORDS:
        return "location"
    # Names that are just locations
    if any(name_lower.startswith(w) for w in ["epstein house", "epstein residence", 
                                               "epstein mansion", "jeffrey epstein house"]):
        return "location"
    # Trusts and legal entities
    if any(name_lower.endswith(w) for w in ["trust", "trust #2", "trust #3", "foundation"]):
        return "org"
    if any(name_lower.startswith(w) for w in ["the ", "the estate"]):
        return "org"
    return "person"

# ── Normalize name for dedup ──
TITLE_PREFIXES = {"mr", "mrs", "ms", "dr", "president", "ceo", "cfo", "chief",
                  "executive", "managing", "director", "attorney", "defendant"}

# Suffixes that indicate extraction artifacts (form field labels, OCR noise)
EXTRACTION_NOISE = re.compile(
    r'\s*-\s*(OAP|TAP|IHD|ARFI|DYS|HY)\s*\w*\s*$'
    r'|\s+Ssn\s*$'
    r'|\s+Inc\s+Air\s*$'
    r'|\s+Mc\s+Air\s*$'
    r'|\s+Air\s+\w+\s*$'
    r'|\s+\(.*?\)\s*$',
    re.IGNORECASE
)

def clean_extraction_name(name):
    """Clean up extraction artifacts from LLM-parsed names."""
    n = name.strip()
    # Remove known suffix noise
    n = EXTRACTION_NOISE.sub('', n).strip()
    # Remove leading/trailing punctuation
    n = n.strip('.,;:\'"- ')
    if len(n) < 3:
        return ""
    return n

def is_fragment_name(name):
    """Detect names that are extraction fragments (not real person names)."""
    n = name.strip()
    parts = n.split()
    if not parts:
        return True
    # Single word that's just a first name or title
    if len(parts) == 1:
        word = parts[0].lower()
        if len(word) <= 3:
            return True
    # Last word is a single letter (initial with no surname)
    if len(parts) >= 2 and len(parts[-1]) == 1:
        return True
    # Last word is very short and looks truncated (2-3 chars, incomplete)
    if len(parts) >= 2 and len(parts[-1]) <= 3:
        # Short last names like Vox, Lee, Fox are valid
        common_short = {'lee','fox','van','von','dee','ray','roe','day','key','rey','bey','jae','rae','mey','lay','hay','loe','nau','vos'}
        return parts[-1].lower() not in common_short
    # Name contains repeated or nonsense patterns
    words_lower = [w.lower() for w in parts]
    # "Nia Xwei", "Nianwell" type OCR garbage
    if any(w in words_lower for w in ['inc air', 'mc air', 'oap ihd']):
        return True
    if any(len(w) > 15 for w in parts):
        return True
    return False

# Blocklist of words/phrases that are clearly not person names
NON_NAME_WORDS = {
    "products", "services", "employees", "members", "officers", "investors",
    "campaign", "administration", "businesses", "transactions", "secret",
    "investigation", "conspiracy", "agreement", "contract", "documents",
    "officials", "representatives", "candidates", "application",
    "offerings", "securities", "placement", "agents", "advisor",
    "investigator", "reporter", "reporters", "journalist", "analyst",
    "specialist", "consultant", "assistant", "coordinator",
    "girl", "girls", "woman", "women", "child", "children", "minor",
    "victim", "victims", "accuser", "accusers", "associate", "associates",
    "card", "account", "accounts", "payment", "payments", "deposit",
    "airport", "airline", "flight", "hotel", "restaurant", "hospital",
    "university", "college", "school", "bank", "banks", "fund",
    "committee", "commission", "agency", "bureau", "department",
    "group", "team", "unit", "division", "office", "firm",
    "international", "national", "global", "regional", "local",
    "official", "officials", "platform", "platforms", "network", "system",
    "program", "project", "initiative", "plan", "policy", "regulation",
}

def is_obviously_bad(name):
    """Filter entries that are clearly not person or org names."""
    n = name.strip()
    if not n:
        return True
    
    # Pure numbers (phone numbers, account numbers)
    if all(c.isdigit() or c in '()- ' for c in n):
        return True
    
    # Starts with lowercase = sentence fragment
    if n[0].islower():
        return True
    
    # Contains common non-name phrases
    n_lower = n.lower()
    
    # Skip orgs with org-indicating words
    org_words = ["center", "institute", "foundation", "university", "college", "school"]
    if any(w in n_lower.split() for w in org_words):
        return False
    
    bad_phrases = [
        "and ", " of ", " to ", " for ", " the ", " in ", " on ", " at ",
        "products and services", "services and products",
        "card ending in", "products and", "and services",
        "members of", "part of", "type of",
    ]
    for phrase in bad_phrases:
        if phrase in n_lower:
            return True
    
    # Last word is in NON_NAME_WORDS (generic descriptor, not a name)
    # Only apply when the entry looks like a sentence fragment (4+ words)
    parts = n.split()
    if parts and parts[-1].lower() in NON_NAME_WORDS and len(parts) > 3:
        return True
    
    # Single word that's a descriptor (e.g. just "Foundation" or "Fund")
    if len(parts) == 1 and parts[0].lower() in NON_NAME_WORDS:
        return True

    # Too many words for a name (likely a phrase)
    if len(parts) > 6:
        return True
    
    # Starts with common sentence-starting words
    sentence_starters = {"the ", "a ", "an ", "this ", "that ", "these ", "those ",
                         "all ", "any ", "each ", "every ", "some ", "many ",
                         "for ", "with ", "without ", "under ", "over ", "through "}
    for s in sentence_starters:
        if n_lower.startswith(s):
            return True
    
    # Has '#' or is an account reference
    if '#' in n or '/' in n:
        # But allow common uses like "J.P. Morgan" - check if it's clearly an identifier
        if not any(c.isalpha() for c in n.replace('#', '').replace('/', '').strip()):
            return True
    
    # Single comma after a word means "is a" construction (e.g. "attorney, the")
    if n.count(',') == 1 and len(n.split(',')[0].split()) <= 2:
        return True
    
    return False

def fuzzy_last_name_match(name1, name2):
    """Check if two names likely refer to the same person by comparing
    first-name start and last-name similarity."""
    p1 = name1.lower().split()
    p2 = name2.lower().split()
    if not p1 or not p2:
        return False
    # Must have at least 2 words to be a proper name
    if len(p1) < 2 or len(p2) < 2:
        return False
    # First name must start the same
    if p1[0][:3] != p2[0][:3]:
        return False
    # Last name (last word) must be similar
    ln1 = p1[-1].rstrip('-')
    ln2 = p2[-1].rstrip('-')
    # Short names: exact match
    if len(ln1) <= 4 or len(ln2) <= 4:
        return ln1 == ln2
    # Long names: check if one starts with the other
    if ln1.startswith(ln2) or ln2.startswith(ln1):
        return True
    # Check first 4 chars of last name
    if ln1[:4] == ln2[:4]:
        return abs(len(ln1) - len(ln2)) <= 3
    # Check if they share a common 4-char substring (catches OCR errors like Axwell/Maxwell)
    # Only match if last names share first 2 chars too (prevents Friedman/Waldman merging)
    if ln1[:2] == ln2[:2]:
        for i in range(len(ln1) - 3):
            if ln1[i:i+4] in ln2:
                return True
    # Final safety: check they share at least one complete word (prevents cascade merges)
    if fuzzy_merged and not set(p1).intersection(set(p2)):
        return False
    return False

def normalize_name(name):
    n = name.lower().strip()
    n = re.sub(r'\(.*?\)', '', n)
    n = n.strip('.,;:()[]\'" ')
    parts = n.split()
    while parts and parts[0] in TITLE_PREFIXES:
        parts = parts[1:]
    suffixes = {"inc", "corp", "llc", "llp", "ltd", "co", "company", "foundation",
                "trust", "group", "holdings", "limited", "international"}
    while parts and parts[-1] in suffixes:
        parts = parts[:-1]
    noise = {"the", "of", "&", "and"}
    parts = [p for p in parts if p not in noise]
    return " ".join(parts) if parts else n

# ── Build canonical name map with type info ──
print("Building canonical name map (dedup)...")
normalized_to_nodes = defaultdict(list)

# Manual canonical overrides for known problematic merges
KNOWN_CANONICALS = {
    "KRISHNA ARVIND": "KRISHNA ARVIND",
    "ARNOLD FRANCES": "ARNOLD FRANCES", 
    "INTERNATIONAL BUSINESS MACHINES": "INTERNATIONAL BUSINESS MACHINES",
    "John Roberts": "John Roberts",
    "John G. Roberts": "John Roberts",
    "John Glover Roberts": "John Roberts",
    "Ketanji Brown Jackson": "Ketanji Brown Jackson",
    "Amy Coney Barrett": "Amy Coney Barrett",
    "Brett Kavanaugh": "Brett Kavanaugh",
    "DOERR L JOHN": "DOERR L JOHN",
    "Arxis": "Arxis",
    "Brown Marianne Catherine": "Brown Marianne Catherine",
    "Marianne Brown": "Brown Marianne Catherine",
    "Pichai Sundar": "Pichai Sundar",
    "WALKER JOHN KENT": "WALKER JOHN KENT",
}

for node_id in g.nodes():
    raw_name = g.nodes[node_id].get("label", "") or str(node_id)
    # Clean extraction artifacts
    name = clean_extraction_name(raw_name)
    if not name or is_fragment_name(name):
        continue
    
    node_type = classify_node(name)
    norm = normalize_name(name)
    normalized_to_nodes[norm].append({
        "node": node_id, "name": name, "raw_name": raw_name, "type": node_type
    })

canonical_map = {}
canonical_index = defaultdict(list)

# ── Fuzzy dedup: merge similar names (OCR variants) ──
print(f"  Running fuzzy dedup on {len(normalized_to_nodes)} groups...")

# First, group by first word for efficiency
from collections import defaultdict as ddict
first_word_groups = ddict(list)
for norm in normalized_to_nodes:
    if not normalized_to_nodes[norm]:
        continue
    first_word = norm.split()[0] if norm.split() else norm
    # Skip KNOWN_CANONICALS entries from fuzzy merge
    entry_names = set(e["name"] for e in normalized_to_nodes[norm] if normalized_to_nodes[norm])
    if entry_names & set(KNOWN_CANONICALS.keys()):
        continue
    first_word_groups[first_word].append(norm)

fuzzy_merged = 0
for first_word, norms in first_word_groups.items():
    if len(norms) < 2:
        continue
    for i in range(len(norms)):
        norm1 = norms[i]
        entries1 = normalized_to_nodes[norm1]
        if not entries1:
            continue
        cand1 = min(entries1, key=lambda e: len(e["name"]))["name"]
        # Skip if either name has a manual override
        names1 = set(e["name"] for e in entries1)
        if names1 & set(KNOWN_CANONICALS.keys()):
            continue
        for j in range(i + 1, len(norms)):
            norm2 = norms[j]
            entries2 = normalized_to_nodes[norm2]
            if not entries2:
                continue
            cand2 = min(entries2, key=lambda e: len(e["name"]))["name"]
            # Skip if either name has a manual override
            names2 = set(e["name"] for e in entries2)
            if names2 & set(KNOWN_CANONICALS.keys()):
                continue
            if fuzzy_last_name_match(cand1, cand2):
                type1 = set(e["type"] for e in entries1)
                type2 = set(e["type"] for e in entries2)
                if "person" in type1 or "person" in type2:
                    if "person" in type1:
                        normalized_to_nodes[norm1].extend(entries2)
                        normalized_to_nodes[norm2] = []
                    else:
                        normalized_to_nodes[norm2].extend(entries1)
                        normalized_to_nodes[norm1] = []
                elif len(entries1) >= len(entries2):
                    normalized_to_nodes[norm1].extend(entries2)
                    normalized_to_nodes[norm2] = []
                else:
                    normalized_to_nodes[norm2].extend(entries1)
                    normalized_to_nodes[norm1] = []
                fuzzy_merged += 1

print(f"  {fuzzy_merged} OCR variants merged")

for norm, entries in normalized_to_nodes.items():
    if not entries:
        continue
    candidates = [e for e in entries if len(e["name"]) > 3]
    if not candidates:
        candidates = entries
    proper = [e for e in candidates if not e["name"].isupper()]
    if proper:
        # Pick the best canonical: prefer longer last name (more complete)
        # then prefer proper capitalization, then shorter total
        def canonical_score(e):
            parts = e["name"].split()
            last_len = len(parts[-1]) if parts else 0
            is_proper = not e["name"].isupper()
            # Known good surnames get a boost
            name_lower = e["name"].lower()
            bonus = 0
            if any(name_lower.endswith(s) for s in ["maxwell", "epstein", "clinton", "trump", "biden"]):
                bonus = 10
            return (last_len + bonus, is_proper, -len(e["name"]))
    
        canonical_entry = max(candidates, key=canonical_score)
    canonical_name = canonical_entry["name"]
    canonical_type = canonical_entry["type"]
    
    # If primary is location but some aliases are person, use person
    types = set(e["type"] for e in entries)
    if "person" in types:
        canonical_type = "person"
    
    for e in entries:
        is_alias = e["name"] != canonical_name
        # Apply manual overrides
        if e["name"] in KNOWN_CANONICALS:
            canonical_name = KNOWN_CANONICALS[e["name"]]
            is_alias = False
        canonical_map[e["node"]] = {
            "canonical": canonical_name,
            "name": e["name"],
            "type": e["type"],
            "is_alias": is_alias,
            "norm": norm,
        }
        if not is_alias:
            canonical_index[canonical_name].append(e)

print(f"  {len(normalized_to_nodes)} unique normalized names")
alias_count = sum(1 for v in canonical_map.values() if v["is_alias"])
print(f"  {alias_count} aliases mapped to canonical names")
location_count = sum(1 for v in canonical_map.values() if v["type"] == "location" and not v["is_alias"])
print(f"  {location_count} location-type entries (filtered from primary search)")

# ── Build search index (excluding locations, phones, trusts) ──
search_index = []
# Build reverse alias index: canonical → list of alias names
rev_alias_index = {}
# Build canonical → list of alias node IDs
rev_alias_nodes = {}
for nid, c_info in canonical_map.items():
    if c_info["is_alias"]:
        canon = c_info["canonical"]
        if canon not in rev_alias_index:
            rev_alias_index[canon] = []
            rev_alias_nodes[canon] = []
        rev_alias_index[canon].append(c_info["name"])
        rev_alias_nodes[canon].append(nid)
print(f"  Built reverse alias index: {len(rev_alias_index)} canonicals with aliases")

# ── Build search index (excluding locations, bad entries) ──
search_index = []
for canonical_name, entries in canonical_index.items():
    entry_type = entries[0]["type"]
    # Skip location-type entries with low connectivity
    if entry_type == "location":
        node_id = entries[0]["node"]
        degree = g.degree(node_id) if g.has_node(node_id) else 0
        if degree < 5:
            continue
    # Skip obviously bad entries
    if is_obviously_bad(canonical_name):
        continue
    
    # Calculate graph degree for this entry
    primary_node = entries[0]["node"]
    entry_degree = g.degree(primary_node) if g.has_node(primary_node) else 0
    
    all_node_ids = [e["node"] for e in entries]
    # Add all alias node IDs from canonical_map
    alias_nids = rev_alias_nodes.get(canonical_name, [])
    for nid in alias_nids:
        if nid not in all_node_ids:
            all_node_ids.append(nid)
    all_names = [canonical_map[nid]["name"] for nid in all_node_ids]
    
    # Collect aliases: names that point to this canonical but aren't in this group
    alias_set = set()
    for nid in all_node_ids:
        cm_entry = canonical_map.get(nid, {})
        if cm_entry.get("is_alias"):
            alias_set.add(cm_entry["name"])
    
    aliases = [n for n in alias_set if n != canonical_name]

    # Also add from reverse index (pre-built)
    if canonical_name in rev_alias_index:
        for a in rev_alias_index[canonical_name]:
            if a not in all_names and a not in aliases:
                aliases.append(a)
    search_index.append({
        "canonical": canonical_name,
        "normalized": normalize_name(canonical_name),
        "type": entry_type,
        "primary_node": entries[0]["node"],
        "all_nodes": all_node_ids,
        "aliases": aliases,
        "count": len(all_node_ids),
        "degree": entry_degree,
    })

# ── Second pass: within each first-name group, keep only the best entry ──
print("  Running second-pass dedup on search index...")
first_name_groups = ddict(list)
for e in search_index:
    parts = e["canonical"].split()
    first_word = parts[0].lower() if parts else e["canonical"].lower()
    first_name_groups[first_word].append(e)

removed = 0
for first_word, entries in first_name_groups.items():
    if len(entries) < 2:
        continue
    # Score each entry: prefer multi-word, longer complete last name, known surnames
    def score(e):
        parts = e["canonical"].split()
        words = len(parts)
        last_len = len(parts[-1]) if parts else 0
        name_lower = e["canonical"].lower()
        surname_bonus = 0
        for s in ["maxwell", "epstein", "clinton", "trump", "biden", "obama", "gates", "bezos", "musk", "buffett"]:
            if name_lower.endswith(s):
                surname_bonus = 20
                break
        if words >= 2 and last_len >= 4:
            return (1, last_len + surname_bonus, -e["count"])
        return (0, 0, 0)
    
    entries.sort(key=score, reverse=True)
    best = entries[0]
    best_last = best["canonical"].split()[-1].lower() if best["canonical"].split() else ""
    
    # Merged aliases into the best entry, but only if last names are similar
    merged_aliases = set(best["aliases"])
    merged_nodes = set(best["all_nodes"])
    merged_count = best["count"]
    
    for e in entries[1:]:
        if e is best:
            continue
        e_last = e["canonical"].split()[-1].lower() if e["canonical"].split() else ""
        # Only merge if last names are similar (share first 3 chars or one contains the other)
        if not (best_last[:3] == e_last[:3] or best_last in e_last or e_last in best_last):
            continue
        merged_aliases.add(e["canonical"])
        merged_aliases.update(e["aliases"])
        merged_nodes.update(e["all_nodes"])
        merged_count += e["count"]
        search_index.remove(e)
        removed += 1
    
    if len(entries) > 1:
        best["aliases"] = [a for a in merged_aliases if a != best["canonical"]]
        best["all_nodes"] = list(merged_nodes)
        best["count"] = merged_count

print(f"  {removed} fragment entries removed (merged into canonical)")
search_index.sort(key=lambda x: x["canonical"].lower())
print(f"  {len(search_index)} canonical entries in search index")

# ── Save search index ──
idx_path = os.path.join(OUT, "search_index.json")
with open(idx_path, "w", encoding="utf-8") as f:
    json.dump(search_index, f, ensure_ascii=False, indent=1)
print(f"Search index saved to {idx_path}")

# ── Save canonical map ──
canon_path = os.path.join(OUT, "canonical_map.json")
with open(canon_path, "w", encoding="utf-8") as f:
    json.dump(canonical_map, f, ensure_ascii=False, indent=1)
print(f"Canonical map saved to {canon_path}")

# ── Save labels ──
labels_path = os.path.join(OUT, "labels.json")
labels = {nid: g.nodes[nid].get("label", str(nid)) for nid in g.nodes()}
with open(labels_path, "w", encoding="utf-8") as f:
    json.dump(labels, f, ensure_ascii=False)
print(f"Labels saved to {labels_path}")

print("\nDone!")
