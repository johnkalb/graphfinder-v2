import csv, io, json, gzip, re, zipfile
from collections import Counter, defaultdict
from pathlib import Path

DOWNLOADS = Path(r"C:\Users\johnk\Downloads")
ZIPS = [DOWNLOADS / "2019.zip", DOWNLOADS / "2024.zip", DOWNLOADS / "2025.zip"]
GRAPH = Path(r"C:\Users\johnk\graphfinder-clean\webapp\data\graph_scored.json.gz")
OUT = Path(r"C:\Users\johnk\graphfinder-clean\uspto_prelim_report.json")


def norm(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"\b(jr|sr)\.?\b", r"\1", s)
    s = re.sub(r"[^a-z0-9' -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s


def looks_org(s: str) -> bool:
    if not s:
        return False
    org_pat = re.compile(r"\b(inc|corp|corporation|company|co\.?|group|foundation|university|college|bank|capital|institute|committee|council|trust|fund|llc|ltd|holdings|partners|associates|school|systems|management|technologies|technology|health|pharma|biotech|laboratories|lab|gmbh|ag|sa|bv|s\.r\.l\.|plc)\b", re.I)
    return bool(org_pat.search(s))


with gzip.open(GRAPH, 'rt', encoding='utf-8') as f:
    g = json.load(f)
node_names = g['nodes']
name_map = {}
for n in node_names:
    k = norm(n)
    if k and k not in name_map:
        name_map[k] = n

total_patents = 0
matched_inventor_occurrences = 0
matched_patents = 0
coinventor_pairs_if_ingested = 0
assigned_to_edges_if_ingested = 0
match_counter = Counter()
assignee_counter = Counter()
examples = []
ambiguous_common_skipped = 0

for zpath in ZIPS:
    with zipfile.ZipFile(zpath) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as raw:
            txt = io.TextIOWrapper(raw, encoding='utf-8', errors='replace', newline='')
            reader = csv.DictReader(txt)
            for row in reader:
                total_patents += 1
                patent_number = row.get('patent_number', '')
                grant_year = row.get('grant_year', '')
                assignee = (row.get('assignee') or '').strip()
                team_size = 0
                try:
                    team_size = int(row.get('team_size') or 0)
                except Exception:
                    team_size = 0

                matched = []
                all_inventors = []
                # inventor_name1.. inventor_nameN
                for k, v in row.items():
                    if k.startswith('inventor_name') and v and v.strip():
                        inv = v.strip()
                        all_inventors.append(inv)
                        nk = norm(inv)
                        hit = name_map.get(nk)
                        if hit:
                            matched.append(hit)

                if not matched:
                    continue

                matched_patents += 1
                matched_inventor_occurrences += len(matched)
                for m in matched:
                    match_counter[m] += 1

                # only co-inventor edges among already matched inventors
                uniq = sorted(set(matched))
                if len(uniq) >= 2:
                    coinventor_pairs_if_ingested += len(uniq) * (len(uniq) - 1) // 2

                if assignee and looks_org(assignee):
                    for _m in uniq:
                        assigned_to_edges_if_ingested += 1
                    assignee_counter[assignee] += len(uniq)

                if len(examples) < 20:
                    examples.append({
                        'patent_number': patent_number,
                        'grant_year': grant_year,
                        'matched_inventors': uniq,
                        'all_inventors_sample': all_inventors[:6],
                        'assignee': assignee,
                        'assignee_looks_org': looks_org(assignee),
                    })

report = {
    'zip_files': [str(p) for p in ZIPS],
    'graph_nodes': len(node_names),
    'total_patents_scanned': total_patents,
    'matched_patents': matched_patents,
    'matched_inventor_occurrences': matched_inventor_occurrences,
    'estimated_coinventor_pairs_if_ingested': coinventor_pairs_if_ingested,
    'estimated_patent_assigned_to_edges_if_ingested': assigned_to_edges_if_ingested,
    'top_matched_inventors': match_counter.most_common(40),
    'top_assignees_from_matched_records': assignee_counter.most_common(40),
    'examples': examples,
}
OUT.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({
    'total_patents_scanned': total_patents,
    'matched_patents': matched_patents,
    'matched_inventor_occurrences': matched_inventor_occurrences,
    'estimated_coinventor_pairs_if_ingested': coinventor_pairs_if_ingested,
    'estimated_patent_assigned_to_edges_if_ingested': assigned_to_edges_if_ingested,
    'report': str(OUT)
}, indent=2))
