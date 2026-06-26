import csv, io, json, re, sqlite3, zipfile
from collections import defaultdict
from pathlib import Path

DOWNLOADS = Path(r"C:\Users\johnk\Downloads")
ZIPS = [DOWNLOADS / "2019.zip", DOWNLOADS / "2024.zip", DOWNLOADS / "2025.zip"]
GRAPH_JSON = Path(r"C:\Users\johnk\graphfinder-clean\webapp\data\graph_scored.json.gz")
DB = Path(r"C:\Users\johnk\data\pipeline_cache.db")
SOURCE_TAG = "USPTO Patents 2019/2024/2025"


def norm(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"\b(jr|sr)\.?\b", r"\1", s)
    s = re.sub(r"[^a-z0-9' -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s


def tokens(name: str):
    return [t for t in norm(name).split() if t]


def looks_org(s: str) -> bool:
    if not s:
        return False
    org_pat = re.compile(r"\b(inc|corp|corporation|company|co\.?|group|foundation|university|college|bank|capital|institute|committee|council|trust|fund|llc|ltd|holdings|partners|associates|school|systems|management|technologies|technology|health|pharma|biotech|laboratories|lab|gmbh|ag|sa|bv|s\.r\.l\.|plc)\b", re.I)
    return bool(org_pat.search(s))


def safe_match(raw_name: str, canonical: str, collision_count: int) -> bool:
    if collision_count != 1:
        return False
    toks = tokens(raw_name)
    if not toks:
        return False
    # very conservative: hyphenated names, or long distinctive surnames,
    # or 3+ token names whose surname is reasonably specific.
    if '-' in raw_name:
        return True
    if len(toks) == 2 and len(toks[-1]) >= 8:
        return True
    if len(toks) >= 3 and len(toks[-1]) >= 7:
        return True
    return False


def load_graph_names():
    import gzip
    with gzip.open(GRAPH_JSON, 'rt', encoding='utf-8') as f:
        data = json.load(f)
    nodes = data['nodes']
    name_to_canonical = {}
    collision = defaultdict(int)
    for n in nodes:
        k = norm(n)
        if not k:
            continue
        collision[k] += 1
        if k not in name_to_canonical:
            name_to_canonical[k] = n
    return name_to_canonical, collision


def main():
    name_to_canonical, collision = load_graph_names()

    coinventor = defaultdict(lambda: {'patents': set(), 'years': set()})
    assigned = defaultdict(lambda: {'patents': set(), 'years': set(), 'assignee': None})

    total_patents = 0
    safe_patents = 0
    safe_inventor_occ = 0

    for zpath in ZIPS:
        with zipfile.ZipFile(zpath) as zf:
            inner = zf.namelist()[0]
            with zf.open(inner) as raw:
                txt = io.TextIOWrapper(raw, encoding='utf-8', errors='replace', newline='')
                reader = csv.DictReader(txt)
                for row in reader:
                    total_patents += 1
                    patent = (row.get('patent_number') or '').strip()
                    year = (row.get('grant_year') or '').strip()
                    assignee = (row.get('assignee') or '').strip()

                    matched = []
                    for k, v in row.items():
                        if k.startswith('inventor_name') and v and v.strip():
                            raw_inv = v.strip()
                            nk = norm(raw_inv)
                            can = name_to_canonical.get(nk)
                            if can and safe_match(raw_inv, can, collision.get(nk, 0)):
                                matched.append(can)

                    uniq = sorted(set(matched))
                    if not uniq:
                        continue

                    safe_patents += 1
                    safe_inventor_occ += len(uniq)

                    # co-inventor only among matched safe inventors
                    if len(uniq) >= 2:
                        for i in range(len(uniq)):
                            for j in range(i + 1, len(uniq)):
                                a, b = sorted((uniq[i], uniq[j]))
                                rec = coinventor[(a, b)]
                                rec['patents'].add(patent)
                                if year:
                                    rec['years'].add(year)

                    # inventor -> assignee org
                    if assignee and looks_org(assignee):
                        for inv in uniq:
                            rec = assigned[(inv, assignee)]
                            rec['assignee'] = assignee
                            rec['patents'].add(patent)
                            if year:
                                rec['years'].add(year)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    # replace prior USPTO rows from this import family
    cur.execute("DELETE FROM relationships WHERE source_data=? AND relation_type IN ('CO_INVENTOR','PATENT_ASSIGNED_TO')", (SOURCE_TAG,))

    inserted_coinventor = 0
    inserted_assigned = 0

    for (a, b), meta in coinventor.items():
        evidence = json.dumps({
            'patent_count': len(meta['patents']),
            'sample_patents': sorted(list(meta['patents']))[:10],
            'years': sorted(list(meta['years']))[:10],
        }, separators=(',', ':'))
        cur.execute(
            "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
            (None, a, 'PERSON', None, b, 'PERSON', 'CO_INVENTOR', SOURCE_TAG, evidence)
        )
        inserted_coinventor += 1

    for (inv, org), meta in assigned.items():
        evidence = json.dumps({
            'patent_count': len(meta['patents']),
            'sample_patents': sorted(list(meta['patents']))[:10],
            'years': sorted(list(meta['years']))[:10],
            'assignee': org,
        }, separators=(',', ':'))
        cur.execute(
            "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
            (None, inv, 'PERSON', None, org, 'ORG', 'PATENT_ASSIGNED_TO', SOURCE_TAG, evidence)
        )
        inserted_assigned += 1

    conn.commit()
    conn.close()

    print(json.dumps({
        'total_patents_scanned': total_patents,
        'safe_patents_used': safe_patents,
        'safe_inventor_occurrences': safe_inventor_occ,
        'coinventor_edges_inserted': inserted_coinventor,
        'patent_assigned_to_edges_inserted': inserted_assigned,
        'source_tag': SOURCE_TAG,
    }, indent=2))


if __name__ == '__main__':
    main()
