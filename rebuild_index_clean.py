"""Rebuild search index directly from DB - bypass graph loading."""
import sqlite3, json
from collections import defaultdict

print("Loading from database...")
conn = sqlite3.connect('data/pipeline_cache.db')
c = conn.cursor()

# Get all distinct nodes from relationships
c.execute('SELECT DISTINCT source_name FROM relationships')
all_sources = [r[0] for r in c.fetchall()]
c.execute('SELECT DISTINCT target_name FROM relationships')
all_targets = [r[0] for r in c.fetchall()]
all_nodes = set(all_sources + all_targets)
print(f"Distinct nodes in DB: {len(all_nodes)}")

def classify_node(name):
    parts = name.split()
    if len(parts) <= 1:
        return "person"
    last = parts[-1].lower()
    if last in ('inc', 'corp', 'llc', 'llp', 'ltd', 'co', 'corporation', 'incorporated', 'company', 'group', 'holdings', 'bank', 'trust', 'fund', 'capital', 'partners'):
        return "company"
    org_words = ['university', 'college', 'institute', 'school', 'foundation', 'center']
    for w in org_words:
        if w in parts:
            return "organization"
    if len(parts) >= 2 and parts[-1][0].isupper() and all(p[0].isupper() or p == '&' for p in parts if p):
        return "person"
    return "entity"

# Get alias relationships
c.execute("SELECT source_name, target_name FROM relationships WHERE relation_type='ALIAS'")
alias_pairs = list(c.fetchall())

# Get degree info
c.execute('SELECT source_name, COUNT(*) as cnt FROM relationships GROUP BY source_name')
degree_src = dict(c.fetchall())
c.execute('SELECT target_name, COUNT(*) as cnt FROM relationships GROUP BY target_name')
degree_tgt = dict(c.fetchall())

def get_degree(name):
    return degree_src.get(name, 0) + degree_tgt.get(name, 0)

# Build index
index = []
alias_map = defaultdict(list)
for alias_name, target_name in alias_pairs:
    alias_map[target_name].append(alias_name)
    alias_map[alias_name].append(target_name)

for node in all_nodes:
    name = node.strip()
    if not name or len(name) < 3:
        continue
    
    node_type = classify_node(name)
    
    # Handle SEC format: LAST FIRST MIDDLE → First Middle Last
    canonical = name
    parts = name.split()
    if len(parts) >= 2 and parts[0].isupper() and len(parts[0]) > 1 and all(p[0].isalpha() for p in parts):
            first = parts[0].title()
            rest = ' '.join(p.title() for p in parts[1:])
            human = f'{rest} {first}'
            if human != name:
                canonical = human
    
    aliases = list(alias_map.get(node, []))
    
    deg = get_degree(name)
    
    index.append({
        'canonical': canonical,
        'aliases': aliases,
        'count': deg,
        'type': node_type,
        'node': node,
    })

# Deduplicate: keep one entry per canonical name
seen = {}
for entry in index:
    canon = entry['canonical']
    if canon in seen:
        existing = seen[canon]
        existing['aliases'].extend(a for a in entry['aliases'] if a not in existing['aliases'])
        existing['count'] = max(existing['count'], entry['count'])
    else:
        seen[canon] = entry

final = list(seen.values())
final.sort(key=lambda e: -e.get('count', 0))

print(f"Index: {len(final)} entries")
with open('webapp/data/search_index.json', 'w') as f:
    json.dump(final, f)

conn.close()
print("Done!")
