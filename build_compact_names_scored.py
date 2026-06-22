"""Build compacted graph name list for client-side contact matching,
from the scored graph (not the legacy pickle).

Output: webapp/data/compact_names.json.gz  ->  { "lowercase name": "Display Name", ... }
"""
import json, gzip, os

with gzip.open('webapp/data/graph_scored.json.gz', 'rt', encoding='utf-8') as f:
    ed = json.load(f)
nodes = ed['nodes']

names = {}
for n in nodes:
    key = n.lower().strip()
    # keep the shortest display form for a given lowercase key
    if key not in names or len(n) < len(names[key]):
        names[key] = n

print(f"Names: {len(names)}")

with gzip.open('webapp/data/compact_names.json.gz', 'wt', encoding='utf-8') as f:
    json.dump(names, f, ensure_ascii=False, separators=(",", ":"))

sz = os.path.getsize('webapp/data/compact_names.json.gz')
print(f"Saved webapp/data/compact_names.json.gz: {sz/1024/1024:.2f} MB")
# sanity
for t in ['donald trump', 'tim cook', 'bill clinton', 'jeffrey epstein']:
    print(f"  {t!r}: {names.get(t, 'MISSING')}")
