import sqlite3
import requests
import json
import os
import sys

DB_PATH = "C:/Users/johnk/data/pipeline_cache.db"

def query_elite_firms(limit=300):
    """Query Wikidata for the top N investment and money manager firms, sorted by importance."""
    url = "https://query.wikidata.org/sparql"
    query = f"""
    SELECT DISTINCT ?firm ?firmLabel ?sitelinks WHERE {{
      # Match firms of interest
      {{
        ?firm wdt:P31 ?class .
        VALUES ?class {{ wd:Q3487908 wd:Q4230006 wd:Q18355352 wd:Q2204741 }}
      }} UNION {{
        ?firm wdt:P452 ?industry .
        VALUES ?industry {{ wd:Q503672 wd:Q220194 wd:Q1271182 }}
      }}
      ?firm wikibase:sitelinks ?sitelinks .
      ?firm rdfs:label ?firmLabel . FILTER(lang(?firmLabel) = 'en')
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language 'en'. }}
    }} ORDER BY DESC(?sitelinks) LIMIT {limit}
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/sparql-results+json'
    }
    print("Fetching elite firm list from Wikidata SPARQL...")
    try:
        res = requests.get(url, params={'query': query}, headers=headers, timeout=45)
        if res.status_code == 200:
            data = res.json()
            bindings = data.get('results', {}).get('bindings', [])
            firms = []
            for row in bindings:
                firms.append({
                    "qid": row['firm']['value'].split('/')[-1],
                    "name": row['firmLabel']['value'],
                    "sitelinks": int(row['sitelinks']['value'])
                })
            return firms
    except Exception as e:
        print("Error querying firm list:", e)
    return []

def query_key_people_batch(qids):
    """Query key people for a batch of QIDs to optimize SPARQL speed and reduce roundtrips."""
    url = "https://query.wikidata.org/sparql"
    values_str = " ".join(f"wd:{q}" for q in qids)
    query = f"""
    SELECT DISTINCT ?firm ?firmLabel ?propLabel ?personLabel ?prop WHERE {{
      VALUES ?firm {{ {values_str} }}
      VALUES ?prop {{ wdt:P112 wdt:P169 wdt:P3320 wdt:P1037 wdt:P26 }} # founder, CEO, board, manager, spouse
      ?firm ?prop ?person .
      ?person rdfs:label ?personLabel . FILTER(lang(?personLabel) = 'en')
      ?firm rdfs:label ?firmLabel . FILTER(lang(?firmLabel) = 'en')
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language 'en'. }}
    }}
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/sparql-results+json'
    }
    relationships = []
    try:
        res = requests.get(url, params={'query': query}, headers=headers, timeout=45)
        if res.status_code == 200:
            data = res.json()
            bindings = data.get('results', {}).get('bindings', [])
            for row in bindings:
                firm_qid = row.get("firm", {}).get("value", "").split('/')[-1]
                firm_name = row.get("firmLabel", {}).get("value")
                person = row.get("personLabel", {}).get("value")
                prop_uri = row.get("prop", {}).get("value", "")
                
                relation = "ASSOCIATE"
                if "P112" in prop_uri:
                    relation = "FOUNDER"
                elif "P169" in prop_uri:
                    relation = "CEO"
                elif "P3320" in prop_uri:
                    relation = "CO_DIRECTOR"
                elif "P1037" in prop_uri:
                    relation = "CO_EXECUTIVE"
                elif "P26" in prop_uri:
                    relation = "FAMILY"
                    
                relationships.append({
                    "source_name": person,
                    "target_name": firm_name,
                    "relation_type": relation,
                    "source_data": f"Wikidata Elite 300 ({firm_qid})"
                })
    except Exception as e:
        print(f"Error querying batch SPARQL: {e}")
    return relationships

def save_to_db(relationships):
    """Write harvested relationships into pipeline_cache.db."""
    if not relationships:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    inserted = 0
    for r in relationships:
        try:
            c.execute("""
                INSERT OR IGNORE INTO relationships (source_name, target_name, relation_type, source_data)
                VALUES (?, ?, ?, ?)
            """, (r["source_name"], r["target_name"], r["relation_type"], r["source_data"]))
            inserted += c.rowcount
        except Exception as e:
            print("DB insert error:", e)
            
    conn.commit()
    conn.close()
    return inserted

def main():
    print("=== Start Harvesting Elite 300 Money Managers ===")
    
    # 1. Fetch top 300 investment firms
    firms = query_elite_firms(300)
    if not firms:
        print("Failed to retrieve any firms. Exiting.")
        return
        
    print(f"Successfully retrieved {len(firms)} elite money manager firms from Wikidata.")
    
    # 2. Query in batches of 15 QIDs to prevent SPARQL timeout and minimize API calls
    all_relationships = []
    batch_size = 15
    total_firms = len(firms)
    
    for i in range(0, total_firms, batch_size):
        batch = firms[i:i+batch_size]
        qids = [f["qid"] for f in batch]
        names = [f["name"] for f in batch]
        
        print(f"Processing batch {i//batch_size + 1}/{((total_firms-1)//batch_size)+1} (firms {i+1} to {min(i+batch_size, total_firms)})...")
        print(f"  Querying key people for: {', '.join(names[:4])}...")
        
        rels = query_key_people_batch(qids)
        print(f"  Found {len(rels)} relationships in batch.")
        all_relationships.extend(rels)
        
    # 3. Save to database
    print(f"\nWriting {len(all_relationships)} extracted connections to database...")
    inserted = save_to_db(all_relationships)
    print(f"Successfully inserted {inserted} new financial/power relationships into {DB_PATH}!")

if __name__ == "__main__":
    main()
