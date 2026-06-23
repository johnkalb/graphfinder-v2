"""QID-based time-overlap engine — SAFE version.

Runs ONLY on people we have already resolved to verified Wikidata QIDs
(qid_map.jsonl, the 1,128 high-degree confirmed identities). For each person we
fetch their dated employment (P108) and education (P69) affiliations, then emit
pairwise edges between people who share an org/school with OVERLAPPING dates.

Because every endpoint is a verified QID mapped to an existing graph node, there
is NO name-matching and NO namesake risk. Edges connect two real graph people.

Output: qid_overlap_edges.jsonl  -> {a, b, rel, org, start, end}
  rel in {SAME_ORG_OVERLAP, SAME_SCHOOL_OVERLAP}
Merged into the scored graph by build_scored_edges.py (already wired to read it).
"""
import requests, json, time, os
from collections import defaultdict
from itertools import combinations

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}
QID_MAP = "qid_map.jsonl"
OUT = "qid_overlap_edges.jsonl"
BATCH = 40           # QIDs per SPARQL query
SLEEP = 1.0


def load_qid_map():
    """name(lower) -> qid, and qid -> display name."""
    name_to_qid, qid_to_name = {}, {}
    with open(QID_MAP, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            name_to_qid[r["name"].lower()] = r["qid"]
            qid_to_name[r["qid"]] = r["name"]
    return name_to_qid, qid_to_name


def fetch_affiliations(qids):
    """For a batch of QIDs, return list of (qid, prop, org_qid, org_label, start, end).
    prop is 'P108' (employer) or 'P69' (educated_at)."""
    values = " ".join(f"wd:{q}" for q in qids)
    query = f"""
SELECT ?person ?prop ?org ?orgLabel ?start ?end WHERE {{
  VALUES ?person {{ {values} }}
  {{ ?person p:P108 ?st . ?st ps:P108 ?org . BIND("P108" AS ?prop) }}
  UNION
  {{ ?person p:P69 ?st . ?st ps:P69 ?org . BIND("P69" AS ?prop) }}
  OPTIONAL {{ ?st pq:P580 ?start . }}
  OPTIONAL {{ ?st pq:P582 ?end . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
"""
    for attempt in range(4):
        try:
            r = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                             headers=HEADERS, timeout=120)
            if r.status_code == 429:
                time.sleep(4 * (attempt + 1)); continue
            rows = r.json()["results"]["bindings"]
            break
        except Exception:
            time.sleep(3 * (attempt + 1))
    else:
        return []
    out = []
    for row in rows:
        person = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
        prop = row.get("prop", {}).get("value", "")
        org = row.get("org", {}).get("value", "").rsplit("/", 1)[-1]
        org_label = row.get("orgLabel", {}).get("value", "")
        start = _yr(row.get("start", {}).get("value", ""))
        end = _yr(row.get("end", {}).get("value", ""))
        if person and org:
            out.append((person, prop, org, org_label, start, end))
    return out


def _yr(s):
    try:
        return int(s[:4]) if s and s[:4].isdigit() else None
    except (ValueError, IndexError):
        return None


def overlaps(a_s, a_e, b_s, b_e):
    """Conservative tenure overlap: missing bounds filled with +/-8yr, plausibility cap."""
    if a_s is None and a_e is None:
        return None
    if b_s is None and b_e is None:
        return None
    a_s2 = a_s if a_s is not None else a_e - 8
    a_e2 = a_e if a_e is not None else a_s + 8
    b_s2 = b_s if b_s is not None else b_e - 8
    b_e2 = b_e if b_e is not None else b_s + 8
    if a_e2 - a_s2 > 50 or b_e2 - b_s2 > 50:
        return None
    if a_s2 <= b_e2 and b_s2 <= a_e2:
        return (max(a_s2, b_s2), min(a_e2, b_e2))
    return None


def main():
    name_to_qid, qid_to_name = load_qid_map()
    qids = list(qid_to_name.keys())
    print(f"Loaded {len(qids)} verified QIDs", flush=True)

    # affiliations[org_qid][prop] = list of (person_qid, start, end)
    affil = defaultdict(lambda: defaultdict(list))
    org_labels = {}
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        rows = fetch_affiliations(batch)
        for person, prop, org, org_label, start, end in rows:
            affil[org][prop].append((person, start, end))
            org_labels[org] = org_label
        time.sleep(SLEEP)
        if i % (BATCH * 5) == 0:
            print(f"  fetched {min(i+BATCH, len(qids))}/{len(qids)} people, "
                  f"{len(affil)} orgs so far", flush=True)

    # emit overlap edges per org
    out_f = open(OUT, "w", encoding="utf-8")
    n_edges = 0
    seen = set()
    for org, by_prop in affil.items():
        for prop, members in by_prop.items():
            rel = "SAME_ORG_OVERLAP" if prop == "P108" else "SAME_SCHOOL_OVERLAP"
            # dedupe members (a person can have the affiliation listed twice)
            for a, b in combinations(members, 2):
                if a[0] == b[0]:
                    continue
                ov = overlaps(a[1], a[2], b[1], b[2])
                if ov is None:
                    continue
                na, nb = qid_to_name.get(a[0]), qid_to_name.get(b[0])
                if not na or not nb:
                    continue
                key = tuple(sorted([na.lower(), nb.lower()]) + [rel])
                if key in seen:
                    continue
                seen.add(key)
                out_f.write(json.dumps({
                    "a": na, "b": nb, "rel": rel,
                    "org": org_labels.get(org, org), "start": ov[0], "end": ov[1],
                }) + "\n")
                n_edges += 1
    out_f.close()
    print(f"DONE: {n_edges} verified overlap edges -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
