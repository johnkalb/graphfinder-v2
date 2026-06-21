"""PoliticianClient: Harvest elected officials from Wikidata SPARQL."""
import requests
from src.utils.logger import logger

WIKIDATA_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "GraphBuilderAdmin admin@graphbuilder.local"

SENATE_QUERY = """
SELECT DISTINCT ?person ?personLabel ?partyLabel WHERE {
  ?person p:P39 ?posStmt .
  ?posStmt ps:P39 wd:Q4416090 .
  ?posStmt pq:P580 ?startTime .
  FILTER(?startTime >= "2019-01-01T00:00:00Z"^^xsd:dateTime)
  OPTIONAL { ?person wdt:P102 ?party . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

HOUSE_QUERY = """
SELECT DISTINCT ?person ?personLabel ?partyLabel WHERE {
  ?person p:P39 ?posStmt .
  ?posStmt ps:P39 wd:Q13218630 .
  ?posStmt pq:P580 ?startTime .
  FILTER(?startTime >= "2019-01-01T00:00:00Z"^^xsd:dateTime)
  OPTIONAL { ?person wdt:P102 ?party . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

GOVERNOR_QUERY = """
SELECT DISTINCT ?person ?personLabel ?partyLabel WHERE {
  ?person p:P39 ?posStmt .
  ?posStmt ps:P39 wd:Q13205023 .
  ?posStmt pq:P580 ?startTime .
  FILTER(?startTime >= "2019-01-01T00:00:00Z"^^xsd:dateTime)
  OPTIONAL { ?person wdt:P102 ?party . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

class PoliticianClient:
    def __init__(self):
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}

    def _query_sparql(self, query, timeout=120):
        """Run a SPARQL query and return results."""
        try:
            res = requests.get(WIKIDATA_ENDPOINT, headers=self.headers,
                               params={"query": query}, timeout=timeout)
            res.raise_for_status()
            data = res.json()
            return data.get("results", {}).get("bindings", [])
        except Exception as e:
            logger.error(f"SPARQL query failed: {e}")
            return []

    def fetch_senate(self):
        """Fetch all US Senators who served since 2019."""
        results = self._query_sparql(SENATE_QUERY)
        seen = set()
        people = []
        for r in results:
            pid = r.get("person", {}).get("value", "")
            if pid and pid not in seen:
                seen.add(pid)
                people.append({
                    "name": r.get("personLabel", {}).get("value", "Unknown"),
                    "party": r.get("partyLabel", {}).get("value", "Unknown") if r.get("partyLabel") else "Unknown",
                    "office": "U.S. Senate"
                })
        logger.info(f"Fetched {len(people)} US Senators from Wikidata")
        return people

    def fetch_house(self):
        """Fetch all US House members who served since 2019."""
        results = self._query_sparql(HOUSE_QUERY)
        seen = set()
        people = []
        for r in results:
            pid = r.get("person", {}).get("value", "")
            if pid and pid not in seen:
                seen.add(pid)
                people.append({
                    "name": r.get("personLabel", {}).get("value", "Unknown"),
                    "party": r.get("partyLabel", {}).get("value", "Unknown") if r.get("partyLabel") else "Unknown",
                    "office": "U.S. House of Representatives"
                })
        logger.info(f"Fetched {len(people)} US House members from Wikidata")
        return people

    def fetch_governors(self):
        """Fetch all US state governors who served since 2019."""
        results = self._query_sparql(GOVERNOR_QUERY)
        seen = set()
        people = []
        for r in results:
            pid = r.get("person", {}).get("value", "")
            if pid and pid not in seen:
                seen.add(pid)
                state = r.get("stateLabel", {}).get("value", "") if r.get("stateLabel") else ""
                people.append({
                    "name": r.get("personLabel", {}).get("value", "Unknown"),
                    "party": r.get("partyLabel", {}).get("value", "Unknown") if r.get("partyLabel") else "Unknown",
                    "office": f"Governor of {state}" if state else "State Governor"
                })
        logger.info(f"Fetched {len(people)} State Governors from Wikidata")
        return people

    def save_politicians(self, db_manager, politicians, relation_type):
        """Save politicians into the relationships table."""
        count = 0
        for p in politicians:
            try:
                db_manager.add_relationship(
                    src_id=None,
                    src_name=p["name"],
                    src_type="PERSON",
                    tgt_id=None,
                    tgt_name=p["office"],
                    tgt_type="GOVERNMENT_BODY",
                    relation=relation_type,
                    source_data="WIKIDATA_POLITICIAN"
                )
                count += 1
            except Exception as e:
                logger.debug(f"Error saving politician {p['name']}: {e}")
        return count

    def process_all(self, db_manager):
        """Fetch and save all politicians."""
        total = 0
        total += self.save_politicians(db_manager, self.fetch_senate(), "MEMBER_OF_CONGRESS")
        total += self.save_politicians(db_manager, self.fetch_house(), "MEMBER_OF_CONGRESS")
        total += self.save_politicians(db_manager, self.fetch_governors(), "GOVERNOR")
        logger.info(f"Saved {total} total politician relationships")
        return total
