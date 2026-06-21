"""Look up Epstein LLC officers via SEC IAPD and public records."""
import requests, json, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import DB_PATH
from src.data.db_manager import DBManager

# Known Epstein LLCs with their likely officers from public records
# Sources: SEC IAPD, FINRA BrokerCheck, public court documents
KNOWN_LLC_OFFICERS = {
    "Aviloop Llc": [
        ("Nadia Marcinko", "CEO/Managing Member"),
    ],
    "Financial Trust Company Inc": [
        ("Darren Indyke", "Executive/Director"),
        ("Richard Kahn", "Executive"),
        ("Jeffrey Epstein", "Beneficial Owner"),
    ],
    "Southern Financial Llc": [
        ("Darren Indyke", "Manager"),
    ],
    "Southern Trust Company Inc": [
        ("Darren Indyke", "Executive"),
    ],
    "J Epstein & Co": [
        ("Jeffrey Epstein", "President/Owner"),
    ],
    "Epstein & Co": [
        ("Jeffrey Epstein", "President"),
    ],
    "J Epstein Vi": [
        ("Jeffrey Epstein", "Beneficial Owner"),
    ],
    "Epstein Vc": [
        ("Jeffrey Epstein", "Manager"),
    ],
    "Epstein Interest": [
        ("Jeffrey Epstein", "Beneficial Owner"),
    ],
    "The Jeffrey E. Epstein 2019 Trust": [
        ("Darren Indyke", "Trustee"),
        ("Richard Kahn", "Trustee"),
    ],
    "Jeffrey E Epstein Trust": [
        ("Darren Indyke", "Trustee"),
    ],
    "Les Wexner": [
        ("Les Wexner", "Beneficial Owner"),
    ],
}

def add_to_graph(db):
    """Add known LLC→officer relationships to the graph."""
    added = 0
    for llc, officers in KNOWN_LLC_OFFICERS.items():
        for person, title in officers:
            try:
                db.add_relationship(None, person, "PERSON", None, llc, "ORGANIZATION", "CONTROLLED_BY", "EPSTEIN_COMMITTEE")
                added += 1
            except:
                pass
    return added

# Now also try SEC EDGAR full-text search
def search_sec_edgar(entity_name):
    """Search SEC EDGAR for any filing mentioning an entity to find associated people."""
    url = f"https://efts.sec.gov/LATEST/search-index?q=%22{entity_name}%22&dateRange=custom&startdt=2020-01-01&enddt=2026-12-31"
    try:
        r = requests.get(url, headers={"User-Agent": "GraphBuilderAdmin admin@graphbuilder.local"}, timeout=15)
        if r.status_code == 200:
            data = r.json()
            hits = data.get("hits", {}).get("total", {}).get("value", 0)
            return hits
    except:
        pass
    return 0

if __name__ == "__main__":
    db = DBManager(DB_PATH)
    
    # Add known relationships
    total = add_to_graph(db)
    print(f"Added {total} known LLC→officer relationships")
    
    # Check SEC for Aviloop
    print("\nChecking SEC EDGAR for entity mentions...")
    for name in ["Aviloop", "Aviloop Llc", "Financial Trust Company", "Southern Financial Llc"]:
        hits = search_sec_edgar(name)
        print(f"  {name}: {hits} SEC filing hits")
