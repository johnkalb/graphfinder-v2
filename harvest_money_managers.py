import sqlite3
import requests
import json
import os

DB_PATH = "C:/Users/johnk/data/pipeline_cache.db"

# Seed list of elite money managers with their descriptive query terms and verified QIDs
MONEY_MANAGERS = [
    {"name": "Citadel LLC", "qid": "Q2974366"},
    {"name": "Bridgewater Associates", "qid": "Q652431"},
    {"name": "Renaissance Technologies", "qid": "Q3424738"},
    {"name": "Elliott Management", "qid": "Q5365696"},
    {"name": "Millennium Management", "qid": "Q65089884"},
    {"name": "Blackstone Group", "qid": "Q880942"},
    {"name": "The Carlyle Group", "qid": "Q926806"},
    {"name": "KKR", "qid": "Q1570773"},
    {"name": "Apollo Global Management", "qid": "Q619121"},
    {"name": "Sequoia Capital", "qid": "Q1852025"},
    {"name": "Andreessen Horowitz", "qid": "Q4034010"},
    {"name": "Founders Fund", "qid": "Q1439864"},
    {"name": "Khosla Ventures", "qid": "Q6402513"},
    {"name": "Point72 Asset Management", "qid": "Q2204741"},
    {"name": "Two Sigma", "qid": "Q18355352"}
]

def search_wikidata_qid(name):
    """Search for the Wikidata QID of a firm name."""
    url = "https://www.wikidata.org/w/api.php"
    params = {
        "action": "wbsearchentities",
        "language": "en",
        "format": "json",
        "search": name
    }
    try:
        res = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if res.status_code == 200:
            results = res.json().get("search", [])
            if results:
                # return first matching QID
                return results[0].get("id")
    except Exception as e:
        print(f"Error searching QID for {name}: {e}")
    return None

def harvest_key_people(qid, firm_name):
    """Query Wikidata SPARQL for founders, CEOs, board members, and executives of the QID."""
    url = "https://query.wikidata.org/sparql"
    query = f"""
    SELECT DISTINCT ?propLabel ?personLabel ?prop WHERE {{
      VALUES ?firm {{ wd:{qid} }}
      VALUES ?prop {{ wdt:P112 wdt:P169 wdt:P3320 wdt:P1037 wdt:P26 }} # founder, CEO, board, manager, spouse
      ?firm ?prop ?person .
      ?person rdfs:label ?personLabel . FILTER(lang(?personLabel) = "en")
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    """
    relationships = []
    try:
        res = requests.get(url, params={"query": query}, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/sparql-results+json"
        }, timeout=30)
        if res.status_code == 200:
            data = res.json()
            bindings = data.get("results", {}).get("bindings", [])
            for row in bindings:
                person = row.get("personLabel", {}).get("value")
                prop_uri = row.get("prop", {}).get("value", "")
                
                # Map Wikidata property to our controlled relationship types
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
                    "source_data": f"Wikidata ({qid})"
                })
    except Exception as e:
        print(f"Error querying SPARQL for {firm_name} ({qid}): {e}")
    return relationships

def save_to_db(relationships):
    """Write harvested relationships into pipeline_cache.db."""
    if not relationships:
        return 0
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # Verify table schema first
    c.execute("PRAGMA table_info(relationships)")
    cols = [r[1] for r in c.fetchall()]
    print("Database columns:", cols)
    
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
    print("=== Start Harvesting Money Managers & Key People ===")
    all_relationships = []
    
    for firm in MONEY_MANAGERS:
        name = firm["name"]
        qid = firm.get("qid")
        if not qid:
            print(f"\nResolving QID for: {name}...")
            qid = search_wikidata_qid(name)
        if not qid:
            print(f"  Could not resolve QID for {name}")
            continue
        print(f"\nProcessing {name} ({qid})...")
        
        print(f"  Querying key people...")
        rels = harvest_key_people(qid, name)
        print(f"  Found {len(rels)} relationships.")
        all_relationships.extend(rels)
        
    print(f"\nWriting {len(all_relationships)} relationships to database...")
    inserted = save_to_db(all_relationships)
    print(f"Successfully inserted {inserted} new financial relationships!")

if __name__ == "__main__":
    main()
