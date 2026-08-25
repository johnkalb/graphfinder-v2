#!/usr/bin/env python3
"""GDELT GKG harvester: process raw GKG files for actor pairs, FOAF mapping, add to graph.
Runs periodically as a cron job to keep the graph enriched with news connections."""
import os, sys, io, csv, json, zipfile, requests, time, pickle, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collections import defaultdict
from src.config import DB_PATH
from src.data.db_manager import DBManager
from fused_name_filter import is_fused_name

CAMEO_FOAF = {"01": ("MENTIONED_WITH", "was mentioned in connection with"),
    "03": ("MENTIONED_WITH", "was mentioned in connection with"),
    "04": ("CONSULTED_WITH", "consulted with"),
    "05": ("COOPERATED_WITH", "cooperated with"),
    "06": ("COOPERATED_WITH", "cooperated with"),
    "07": ("PROVIDED_AID_TO", "provided aid to"),
    "16": ("MENTIONED_WITH", "was mentioned in connection with"),}
DEFAULT_FOAF = ("MENTIONED_WITH", "was mentioned in connection with")

def load_graph_nodes():
    path = os.path.join(os.path.dirname(__file__), "webapp", "data", "graph.pkl")
    with open(path, "rb") as f:
        g = pickle.load(f)
    nodes = {}
    for node in g.nodes():
        label = g.nodes[node].get("label", node)
        nodes[label.lower()] = label
    return nodes


def fetch_event_lookup(gkg_url):
    """Download the Events export file matching a GKG file's timestamp
    (same 15-min interval, published as a sibling file: NNNN.gkg.csv.zip
    alongside NNNN.export.CSV.zip) and build SOURCEURL -> EventRootCode.

    This is what actually operationalizes CAMEO_FOAF: GDELT's own
    TABARI/PETRARCH NLP already classified the actor interaction in each
    event record. But GDELT's actor coding resolves to generic role/country
    tokens (Actor1Name="PRESIDENT", "GOV", country codes, etc.) for the
    large majority of events, not the specific named individuals GKG's
    free-text Persons extraction finds -- so this is a best-effort,
    document-level signal (the article the two people co-occur in also
    contains a coded event), not a verified per-pair match to the exact two
    people. Real per-pair "why" still needs article-text classification;
    see gdelt_gkg_harvester's caller-side notes.
    """
    export_url = gkg_url.replace(".gkg.csv.zip", ".export.CSV.zip")
    try:
        r = requests.get(export_url, timeout=120)
        if r.status_code != 200:
            return {}
    except Exception:
        return {}
    lookup = {}
    try:
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            with zf.open(zf.namelist()[0]) as f:
                reader = csv.reader(io.TextIOWrapper(f, 'utf-8', errors='replace'), delimiter='\t')
                for row in reader:
                    if len(row) < 61:
                        continue
                    source_url = row[60]
                    event_root = row[28]
                    if source_url and source_url not in lookup and event_root in CAMEO_FOAF:
                        lookup[source_url] = event_root
    except Exception:
        return {}
    return lookup


def scan_gkg_file(url, known_nodes, db):
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        return 0
    added = 0
    import csv as _csv
    _csv.field_size_limit(500000)
    event_lookup = fetch_event_lookup(url)
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            reader = csv.reader(io.TextIOWrapper(f, 'utf-8', errors='replace'), delimiter='\t')
            for i, row in enumerate(reader):
                if i == 0:
                    continue
                if i > 20000:
                    break
                persons = row[11].lower() if len(row) > 11 and row[11] else ''
                if not persons:
                    continue
                # Require full-name matches only -- known_nodes used to also
                # register any single word >=5 chars from an existing node
                # label as a standalone match, which is how GDELT NER noise
                # ("whatsapp linkedin", "our lady") slipped through: those
                # garbled extractions coincidentally matched a stray keyword
                # from a real node's label. Full lowercase-name equality
                # against known_nodes is a much tighter bar.
                person_list = [p.strip() for p in row[11].split(';') if p.strip()]
                matched = [p for p in person_list if p.lower() in known_nodes and not is_fused_name(p)]
                if len(matched) < 2:
                    continue

                doc_url = row[4] if len(row) > 4 else ''
                doc_date = row[1] if len(row) > 1 else ''
                doc_themes = row[8] if len(row) > 8 else ''
                event_root = event_lookup.get(doc_url)
                relation_type, _ = CAMEO_FOAF.get(event_root, DEFAULT_FOAF)
                evidence = json.dumps({
                    "source_url": doc_url,
                    "date": doc_date,
                    "themes": [t for t in doc_themes.split(';') if t][:10],
                    "event_root_code": event_root,
                })

                for p1 in matched:
                    for p2 in matched:
                        if p1.lower() < p2.lower() and p1.lower() != p2.lower():
                            try:
                                db.add_relationship(None, p1, "PERSON", None, p2, "PERSON",
                                    relation_type, "GDELT", evidence)
                                added += 1
                            except:
                                pass
    return added

def main():
    print("Loading graph...")
    known_nodes = load_graph_nodes()
    print(f"Graph: {len(known_nodes)} keywords")
    
    print("Fetching GDELT master file list...")
    r = requests.get('http://data.gdeltproject.org/gdeltv2/masterfilelist.txt', timeout=120)
    lines = r.text.strip().split('\n')
    
    # Scan 1 day per month for the last 5 years (60 files total)
    # Gives broad coverage: Epstein arrest Jul 2019, death Aug 2019,
    # trials 2021, conviction 2022, and ongoing coverage
    sampled_dates = set()
    import datetime
    today = datetime.date.today()
    for months_ago in range(60):
        # Pick the 15th of each month (or nearest available)
        target = today - datetime.timedelta(days=months_ago * 30)
        month_str = target.strftime('%Y%m')
        sampled_dates.add(month_str)
    
    db = DBManager(DB_PATH)
    total_added = 0
    files_scanned = 0
    
    print(f"Sampling 1 day from each of {len(sampled_dates)} months...")
    
    for month in sorted(sampled_dates):
        # Find a file from the 15th of this month
        day15 = month + '15'
        day_files = [l for l in lines if '/' + day15 in l and 'gkg.csv.zip' in l]
        if not day_files:
            day_files = [l for l in lines if '/' + month in l and 'gkg.csv.zip' in l]
            if not day_files:
                continue
            # Pick from available days
            day_files = [day_files[0]]
        
        # Pick one file from midday
        file_entry = day_files[len(day_files)//2]
        url = file_entry.split(' ')[-1]
        fname = url.rsplit('/', 1)[-1]
        print(f"  [{month}] {fname}...")
        added = scan_gkg_file(url, known_nodes, db)
        total_added += added
        files_scanned += 1
        if added > 0:
            print(f"    +{added} relationships")
        time.sleep(1)
    
    print(f"\nScanned {files_scanned} GKG files across {len(sampled_dates)} months")
    print(f"Total new relationships: {total_added}")
    
    if total_added > 0:
        print("Rebuilding search index...")
        subprocess.run([sys.executable, "build_index.py"], capture_output=True)
        print("Restarting web app...")
        subprocess.run(["taskkill", "/F", "/IM", "uvicorn"], capture_output=True)
        time.sleep(2)
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "pathfinder:app", "--host", "0.0.0.0", "--port", "8000"],
            cwd=os.path.join(os.path.dirname(__file__), "webapp"))
        print("App restarted.")

if __name__ == "__main__":
    main()
