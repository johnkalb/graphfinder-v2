"""Enrich with Wikidata employment data via entity search API."""
import sys, os, pickle, requests, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from src.data.db_manager import DBManager
from src.config import DB_PATH

db = DBManager(DB_PATH)
HEADERS = {'User-Agent': 'GraphBuilderAdmin/1.0'}

with open('webapp/data/graph.pkl', 'rb') as f:
    g = pickle.load(f)

people = [n for n in g.nodes() if g.nodes[n].get('type') == 'PERSON' and not n.isupper() and len(n.split()) >= 2]
# Only real names (not SEC-formatted)
real_names = [n for n in people if n[0].isupper() and n.split()[0][-1] != '.' and len(n.split()[0]) > 2]
print(f"Person nodes: {len(real_names)}")

total = 0
batch_count = 0

# Limit to first 1000 for the demo
for idx, name in enumerate(real_names[:1000]):
    time.sleep(0.4)
    
    # Search Wikidata for this person
    try:
        r = requests.get('https://www.wikidata.org/w/api.php', params={
            'action':'wbsearchentities','search':name,'language':'en','format':'json','limit':1
        }, headers=HEADERS, timeout=10)
        if r.status_code != 200:
            continue
        results = r.json().get('search',[])
        if not results:
            continue
        qid = results[0]['id']
        label = results[0]['label']
        
        # Normalize - skip if labels don't match well
        if label.lower() != name.lower() and label.split(',')[0].lower() != name.lower():
            continue
        
        # Get claims
        r2 = requests.get(f'https://www.wikidata.org/wiki/Special:EntityData/{qid}.json', headers=HEADERS, timeout=10)
        claims = r2.json().get('entities',{}).get(qid,{}).get('claims',{})
        
        # Extract employer (P108), board (P512), education (P69)
        props = {'P108': 'EMPLOYER', 'P512': 'BOARD_MEMBER_OF', 'P69': 'ALUMNI_OF'}
        for pid, rtype in props.items():
            if pid not in claims:
                continue
            for c in claims[pid]:
                snak = c.get('mainsnak',{})
                if snak.get('datatype') != 'wikibase-item':
                    continue
                val = snak.get('datavalue',{}).get('value',{}).get('id','')
                if not val:
                    continue
                # Get the label of the organization
                r3 = requests.get(f'https://www.wikidata.org/wiki/Special:EntityData/{val}.json', headers=HEADERS, timeout=5)
                org_label = list(r3.json().get('entities',{}).values())[0].get('labels',{}).get('en',{}).get('value','')
                if not org_label or org_label == name:
                    continue
                db.add_relationship(None, name, 'PERSON', None, org_label, 'ORGANIZATION', rtype, 'WIKIDATA')
                total += 1
        
    except Exception as e:
        continue
    
    if (idx + 1) % 50 == 0:
        print(f'  Processed {idx+1}, found {total} edges')
        time.sleep(2)  # Extra delay every 50

print(f'\nTotal new edges: {total}')
