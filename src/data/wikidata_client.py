import time
import requests
from src.config import WIKIDATA_USER_AGENT
from src.utils.logger import logger

class WikidataClient:
    def __init__(self, rate_limit_per_sec=1.0):
        self.endpoint = "https://query.wikidata.org/sparql"
        self.headers = {
            "User-Agent": WIKIDATA_USER_AGENT,
            "Accept": "application/sparql-results+json"
        }
        self.rate_limit_per_sec = rate_limit_per_sec
        self.last_req_time = 0.0

    def _wait_for_rate_limit(self):
        if self.rate_limit_per_sec <= 0:
            return
        min_spacing = 1.0 / self.rate_limit_per_sec
        elapsed = time.time() - self.last_req_time
        if elapsed < min_spacing:
            time.sleep(min_spacing - elapsed)
        self.last_req_time = time.time()

    def fetch_person_relationships(self, person_name):
        logger.info(f"Querying Wikidata for relationships of: {person_name}")
        
        # Escape quotes for safety in SPARQL query
        escaped_name = person_name.replace('"', '\\"')
        
        # SPARQL Query with standard prefixes and label service
        query = f"""
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX wikibase: <http://wikiba.se/ontology#>
        PREFIX bd: <http://www.bigdata.com/rdf#>

        SELECT ?personLabel ?relationLabel ?targetLabel WHERE {{
          ?person rdfs:label "{escaped_name}"@en .
          ?person ?property ?target .
          
          # Map properties to readable names
          VALUES (?property ?relationLabel) {{
            (wdt:P26 "SPOUSE")
            (wdt:P108 "EMPLOYER")
            (wdt:P463 "MEMBER_OF")
            (wdt:P1411 "AWARD_NOMINEE")
            (wdt:P169 "CHIEF_EXECUTIVE")
            (wdt:P69 "ALMA_MATER")
          }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }} LIMIT 50
        """
        
        self._wait_for_rate_limit()
        try:
            res = requests.get(
                self.endpoint, 
                headers=self.headers, 
                params={"query": query}, 
                timeout=15
            )
            res.raise_for_status()
            data = res.json()
            
            results = []
            for row in data.get("results", {}).get("bindings", []):
                # Ensure the fields exist and extract their values
                person_label = row.get("personLabel", {}).get("value", person_name)
                relation_label = row.get("relationLabel", {}).get("value", "UNKNOWN")
                target_label = row.get("targetLabel", {}).get("value", "")
                
                if target_label:
                    results.append({
                        "source_name": person_label,
                        "relation_type": relation_label,
                        "target_name": target_label,
                    })
            logger.debug(f"Discovered {len(results)} relations on Wikidata for {person_name}")
            return results
        except Exception as e:
            logger.error(f"Wikidata SPARQL query failed for {person_name}: {e}")
            return []
