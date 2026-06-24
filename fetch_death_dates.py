"""Query Wikidata P570 (date of death) for our verified-QID people.

Uses the existing qid_map.jsonl (1,128 namesake-safe QIDs). For each, a single
batched SPARQL query returns date of death if present. Output: deceased.json
  { "lowercase name": "YYYY-MM-DD" or "YYYY", ... } for people known to be dead.

Authoritative (Wikidata structured data) — NOT GDELT text guessing.
"""
import requests, json, time
from collections import defaultdict

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}
QID_MAP = "qid_map.jsonl"
OUT = "deceased.json"
BATCH = 60


def main():
    qid_to_name = {}
    with open(QID_MAP, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            qid_to_name[r["qid"]] = r["name"]
    qids = list(qid_to_name.keys())
    print(f"Checking death dates for {len(qids)} QIDs", flush=True)

    deceased = {}
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        values = " ".join(f"wd:{q}" for q in batch)
        query = f"""
SELECT ?person ?death WHERE {{
  VALUES ?person {{ {values} }}
  ?person wdt:P570 ?death .
}}
"""
        for attempt in range(4):
            try:
                resp = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                                    headers=HEADERS, timeout=90)
                if resp.status_code == 429:
                    time.sleep(4 * (attempt + 1)); continue
                rows = resp.json()["results"]["bindings"]
                break
            except Exception:
                time.sleep(3 * (attempt + 1))
        else:
            rows = []
        for row in rows:
            qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
            death = row.get("death", {}).get("value", "")[:10]
            name = qid_to_name.get(qid)
            if name and death:
                deceased[name.lower()] = death
        time.sleep(1.0)
        print(f"  {min(i+BATCH, len(qids))}/{len(qids)} checked, {len(deceased)} deceased so far", flush=True)

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(deceased, f, indent=0, sort_keys=True)
    print(f"DONE: {len(deceased)} deceased people -> {OUT}", flush=True)
    # show a sample
    for nm in list(deceased)[:12]:
        print(f"  {nm} (d. {deceased[nm]})")


if __name__ == "__main__":
    main()
