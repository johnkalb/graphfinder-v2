import csv, io, json, gzip, re, zipfile
from collections import Counter, defaultdict
from pathlib import Path

DOWNLOADS = Path(r"C:\Users\johnk\Downloads")
ZIPS = [DOWNLOADS / "2019.zip", DOWNLOADS / "2024.zip", DOWNLOADS / "2025.zip"]
GRAPH = Path(r"C:\Users\johnk\graphfinder-clean\webapp\data\graph_scored.json.gz")
OUT = Path(r"C:\Users\johnk\graphfinder-clean\uspto_high_confidence_report.json")


def norm(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("’", "'")
    s = re.sub(r"\b(jr|sr)\.?\b", r"\1", s)
    s = re.sub(r"[^a-z0-9' -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s


def token_sig(name: str):
    toks = [t for t in norm(name).split() if t]
    return toks


def looks_org(s: str) -> bool:
    if not s:
        return False
    org_pat = re.compile(r"\b(inc|corp|corporation|company|co\.?|group|foundation|university|college|bank|capital|institute|committee|council|trust|fund|llc|ltd|holdings|partners|associates|school|systems|management|technologies|technology|health|pharma|biotech|laboratories|lab|gmbh|ag|sa|bv|s\.r\.l\.|plc)\b", re.I)
    return bool(org_pat.search(s))


def distinctive_name(name: str) -> bool:
    toks = token_sig(name)
    if len(toks) < 2:
        return False
    # Strong if has 3+ tokens OR at least one long uncommon-ish token
    if len(toks) >= 3:
        return True
    return any(len(t) >= 8 for t in toks)


with gzip.open(GRAPH, 'rt', encoding='utf-8') as f:
    g = json.load(f)
node_names = g['nodes']
edges = g['edges']

name_to_canonical = {}
canonical_degree = Counter()
for i, n in enumerate(node_names):
    k = norm(n)
    if k and k not in name_to_canonical:
        name_to_canonical[k] = n
for u, v, *_ in edges:
    canonical_degree[node_names[u]] += 1
    canonical_degree[node_names[v]] += 1

# how many graph nodes share same normalized name?
name_collision_count = Counter(norm(n) for n in node_names if norm(n))

def is_high_confidence(inventor_raw: str, matched_name: str) -> bool:
    nk = norm(inventor_raw)
    if not nk or not matched_name:
        return False
    # must be unique normalized match in graph
    if name_collision_count[nk] != 1:
        return False
    # distinctive names are safe; otherwise require high-degree notable node
    if distinctive_name(inventor_raw):
        return True
    return canonical_degree.get(matched_name, 0) >= 300


total_patents = 0
hc_patents = 0
hc_inventor_occurrences = 0
coinventor_pairs = 0
assigned_to_edges = 0
match_counter = Counter()
assignee_counter = Counter()
examples = []
skipped_ambiguous = 0
skipped_non_org_assignee = 0

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

                matched = []
                for k, v in row.items():
                    if k.startswith('inventor_name') and v and v.strip():
                        inv = v.strip()
                        nk = norm(inv)
                        hit = name_to_canonical.get(nk)
                        if hit:
                            if is_high_confidence(inv, hit):
                                matched.append(hit)
                            else:
                                skipped_ambiguous += 1

                uniq = sorted(set(matched))
                if not uniq:
                    continue

                hc_patents += 1
                hc_inventor_occurrences += len(uniq)
                for m in uniq:
                    match_counter[m] += 1

                if len(uniq) >= 2:
                    coinventor_pairs += len(uniq) * (len(uniq) - 1) // 2

                if assignee and looks_org(assignee):
                    assigned_to_edges += len(uniq)
                    assignee_counter[assignee] += len(uniq)
                else:
                    skipped_non_org_assignee += len(uniq)

                if len(examples) < 25:
                    examples.append({
                        'patent_number': patent_number,
                        'grant_year': grant_year,
                        'matched_inventors': uniq,
                        'assignee': assignee,
                        'assignee_looks_org': looks_org(assignee),
                    })

report = {
    'total_patents_scanned': total_patents,
    'high_confidence_patents': hc_patents,
    'high_confidence_inventor_occurrences': hc_inventor_occurrences,
    'estimated_coinventor_pairs_if_ingested': coinventor_pairs,
    'estimated_patent_assigned_to_edges_if_ingested': assigned_to_edges,
    'skipped_ambiguous_matches': skipped_ambiguous,
    'skipped_non_org_assignee_edges': skipped_non_org_assignee,
    'top_high_confidence_inventors': match_counter.most_common(60),
    'top_assignees_from_high_confidence_records': assignee_counter.most_common(60),
    'examples': examples,
}
OUT.write_text(json.dumps(report, indent=2), encoding='utf-8')
print(json.dumps({
    'high_confidence_patents': hc_patents,
    'high_confidence_inventor_occurrences': hc_inventor_occurrences,
    'estimated_coinventor_pairs_if_ingested': coinventor_pairs,
    'estimated_patent_assigned_to_edges_if_ingested': assigned_to_edges,
    'skipped_ambiguous_matches': skipped_ambiguous,
    'report': str(OUT)
}, indent=2))
