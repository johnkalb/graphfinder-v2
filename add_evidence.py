"""Add evidence links to edge types: TRANSACTED_WITH, MENTIONED_WITH, COMMUNICATED_WITH, 
TRAVELED_TO, ASSOCIATED_WITH, EMPLOYED_BY, MET_WITH, GRANTED_TO, AFFILIATED_WITH, 
CONTROLLED_BY, PROMOTER, PROMOTER_D"""
import sqlite3, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

# Add evidence column to pipeline_cache if not exists
conn = sqlite3.connect('data/pipeline_cache.db')
c = conn.cursor()
try:
    c.execute('ALTER TABLE relationships ADD COLUMN evidence TEXT')
    print('Added evidence column')
except:
    print('Evidence column already exists')

# Populate from graph_canonical.db for Epstein edges
epstein_types = ['TRANSACTED_WITH','COMMUNICATED_WITH','TRAVELED_TO','ASSOCIATED_WITH',
                 'EMPLOYED_BY','MET_WITH','AFFILIATED_WITH','CONTROLLED_BY']

# Open Epstein DB
try:
    ep = sqlite3.connect('epstein_data/graph_canonical.db')
    epc = ep.cursor()
    
    updated = 0
    for etype in epstein_types:
        # Get all edges of this type from graph_canonical
        epc.execute('SELECT source_label, target_label, evidence_json FROM edges WHERE edge_type = ?', (etype,))
        for src, tgt, ev_json in epc.fetchall():
            try:
                ev = json.loads(ev_json)
                # Store doc_id and snippet
                evidence = json.dumps([{'doc_id': e.get('doc_id'), 'snippet': e.get('snippet','')[:100]} for e in ev[:3]])
                c.execute('UPDATE relationships SET evidence = ? WHERE source_name = ? AND target_name = ? AND relation_type = ? AND source_data = ?',
                         (evidence, src, tgt, etype, 'EPSTEIN_COMMITTEE'))
                updated += c.rowcount
            except:
                pass
        print(f'{etype}: {updated} edges updated (cumulative)')
    
    ep.close()
except Exception as e:
    print(f'Error reading Epstein DB: {e}')

# For GDELT MENTIONED_WITH - links to news articles mentioned in GKG
# We'll add a placeholder URL pattern
c.execute("UPDATE relationships SET evidence = '{\"note\": \"News article co-mention from GDELT GKG database\"}' WHERE relation_type = 'MENTIONED_WITH' AND evidence IS NULL")
print(f'GDELT MENTIONED_WITH: {c.rowcount} marked')

# For GRANTED_TO (IRS) - add filing URL
c.execute("UPDATE relationships SET evidence = '{\"note\": \"IRS Form 990 filing - ProPublica Nonprofit Explorer\"}' WHERE relation_type = 'GRANTED_TO' AND evidence IS NULL")
print(f'IRS GRANTED_TO: {c.rowcount} marked')

conn.commit()
conn.close()
print('\nDone')
