"""Add remaining cabinet secretaries and judges via Wikidata."""
import sys, os, requests, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from src.config import DB_PATH
from src.data.db_manager import DBManager

HEADERS = {"User-Agent": "GraphBuilderAdmin/1.0"}
SPARQL = "https://query.wikidata.org/sparql"
db = DBManager(DB_PATH)
total = 0

# People who didn't get processed via Wikipedia
remaining = [
    # Trump cabinet
    "Mike Pompeo", "Rex Tillerson", "James Mattis", "Mark Esper",
    "Steve Mnuchin", "Jeff Sessions", "William Barr", "Kirstjen Nielsen",
    "Ryan Zinke", "David Bernhardt", "Sonny Perdue", "Alex Azar",
    "Tom Price", "Betsy DeVos", "Ben Carson", "Rick Perry",
    "Dan Brouillette", "Scott Pruitt", "Andrew Wheeler", "Wilbur Ross",
    "Robert Lighthizer", "Mark Meadows", "Robert O'Brien", "John R. Bolton",
    "H. R. McMaster", "Michael T. Flynn", "Linda McMahon", "Eugene Scalia",
    # Biden cabinet
    "Antony Blinken", "Lloyd Austin", "Janet Yellen", "Merrick Garland",
    "Deb Haaland", "Tom Vilsack", "Gina Raimondo", "Xavier Becerra",
    "Miguel Cardona", "Jennifer Granholm", "Pete Buttigieg", "Marcia Fudge",
    "Denis McDonough", "Michael Regan", "Katherine Tai", "Alejandro Mayorkas",
    "Avril Haines", "Jeff Zients", "Karine Jean-Pierre",
    # Trump judges
    "Neil Gorsuch", "Brett Kavanaugh", "Amy Coney Barrett",
    "Don Willett", "James Ho", "Kyle Duncan", "Andrew Oldham",
    "Neomi Rao", "Justin R. Walker", "Amul Thapar", "John K. Bush",
    "Joan Larsen", "Stephanos Bibas", "David J. Porter",
    # Biden judges
    "Ketanji Brown Jackson", "J. Michelle Childs", "Eunice C. Lee",
    "Myrna Pérez", "Alison J. Nathan", "Dale Ho",
]

def query_wd(label):
    time.sleep(1.0)
    r = requests.get("https://www.wikidata.org/w/api.php",
        params={"action":"wbsearchentities","search":label,"language":"en","format":"json","limit":2},
        headers=HEADERS, timeout=10)
    if r.status_code != 200:
        return None, []
    results = r.json().get("search", [])
    if not results:
        return None, []
    qid = results[0]["id"]
    
    # Get position held, employer, educated at
    q = f"""
    SELECT ?prop ?propLabel ?val ?valLabel WHERE {{
      VALUES ?props {{ wdt:P39 wdt:P108 wdt:P69 wdt:P1416 wdt:P512 wdt:P802 }}
      wd:{qid} ?props ?val .
      BIND(STR(?props) AS ?prop)
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
    }}
    """
    r2 = requests.get(SPARQL, params={"format":"json","query":q}, headers=HEADERS, timeout=15)
    if r2.status_code != 200:
        return qid, []
    bindings = r2.json().get("results",{}).get("bindings",[])
    return qid, bindings

role_map = {
    "http://www.wikidata.org/prop/direct/P39": "HELD",
    "http://www.wikidata.org/prop/direct/P108": "EMPLOYED_BY",
    "http://www.wikidata.org/prop/direct/P69": "EDUCATED_AT",
    "http://www.wikidata.org/prop/direct/P1416": "AFFILIATED_WITH",
    "http://www.wikidata.org/prop/direct/P512": "ACADEMIC_DEGREE",
    "http://www.wikidata.org/prop/direct/P802": "STUDENT",
}

for i, name in enumerate(remaining):
    qid, bindings = query_wd(name)
    if not bindings:
        print(f"  [{i+1:3d}] {name[:40]:40s} → no data")
        continue
    
    if total <= 10:
        print(f"  [{i+1:3d}] {name[:40]:40s} → {qid}")
    
    for item in bindings:
        prop = item.get("prop",{}).get("value","")
        role = role_map.get(prop, "AFFILIATED")
        val = item.get("valLabel",{}).get("value","")
        if val and len(val) > 5:
            try:
                if prop.endswith("/P39"):
                    entity_type = "POSITION"
                elif prop.endswith("/P108") or prop.endswith("/P1416"):
                    entity_type = "ORGANIZATION"
                elif prop.endswith("/P69") or prop.endswith("/P512"):
                    entity_type = "INSTITUTION"
                else:
                    entity_type = "RELATED"
                db.add_relationship(None, name, "PERSON", None, val, entity_type, role, "WIKIDATA")
                total += 1
            except:
                pass
    
    if (i+1) % 25 == 0:
        print(f"  Progress: {i+1}/{len(remaining)}, {total} added")

print(f"\n\nTotal added: {total}")
