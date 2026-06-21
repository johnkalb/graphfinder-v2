#!/usr/bin/env python3
"""GDELT GKG harvester: process raw GKG files for actor pairs, FOAF mapping, add to graph.
Runs periodically as a cron job to keep the graph enriched with news connections."""
import os, sys, io, csv, zipfile, requests, time, pickle, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collections import defaultdict
from src.config import DB_PATH
from src.data.db_manager import DBManager

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
        for word in label.lower().split():
            if len(word) > 4:
                nodes[word] = word
    return nodes

def scan_gkg_file(url, known_nodes, db):
    r = requests.get(url, timeout=120)
    if r.status_code != 200:
        return 0
    added = 0
    import csv as _csv
    _csv.field_size_limit(500000)
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
                person_list = [p.strip() for p in row[11].split(';') if p.strip()]
                matched = [p for p in person_list if p.lower() in known_nodes]
                if len(matched) >= 2:
                    for p1 in matched:
                        for p2 in matched:
                            if p1.lower() < p2.lower() and p1.lower() != p2.lower():
                                try:
                                    db.add_relationship(None, p1, "PERSON", None, p2, "PERSON",
                                        DEFAULT_FOAF[0], "GDELT")
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
