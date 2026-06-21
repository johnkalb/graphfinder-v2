#!/usr/bin/env python3
"""Level A: Extract boards from all unfilled foundations."""
import os, sys, json, time, requests, xml.etree.ElementTree as ET
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.config import DB_PATH
from src.data.db_manager import DBManager

HEADERS = {"User-Agent": "GraphBuilderAdmin admin@graphbuilder.local"}
NS = "http://www.irs.gov/efile"
PROPUBLICA_SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
XML_TPL = "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{oid}_public.xml"

# Keywords to find foundations
KEYWORDS = ["foundation", "foundation"]  # Will deduplicate

# Skip these — already processed
ALREADY_DONE = {
    "131684331",  # Ford Foundation
    "237093598",  # MacArthur Foundation
    "852361213",  # Lehmann Family Foundation
    "136088860",  # Overbrook Foundation
}

def get_board(oid, name):
    """Extract board members from a foundation's Form 990."""
    try:
        r = requests.get(XML_TPL.format(oid=oid), headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except:
        return []

    board = []
    # OfficerDirTrstKeyEmplGrp (Form 990 — full board)
    for grp in root.iter(f"{{{NS}}}OfficerDirTrstKeyEmplGrp"):
        nm = ""
        title = ""
        for child in grp.iter():
            ct = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ct == "PersonNm" and child.text: nm = child.text.strip()
            elif ct == "TitleTxt" and child.text: title = child.text.strip()
        if nm: board.append((nm, title))

    # BusinessOfficerGrp (Form 990-PF — principal officer)
    if not board:
        for grp in root.iter(f"{{{NS}}}BusinessOfficerGrp"):
            nm = ""
            title = ""
            for child in grp.iter():
                ct = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ct == "PersonNm" and child.text: nm = child.text.strip()
                elif ct == "PersonTitleTxt" and child.text: title = child.text.strip()
            if nm: board.append((nm, title))

    return board

def main():
    db = DBManager(DB_PATH)
    start = time.time()

    # Step 1: Search for foundations
    print("Searching for foundations...")
    all_orgs = []
    seen_eins = set()
    for kw in KEYWORDS:
        r = requests.get(PROPUBLICA_SEARCH, headers=HEADERS, params={"q": kw}, timeout=15)
        if r.status_code == 200:
            for o in r.json().get("organizations", []):
                ein = str(o.get("ein", ""))
                if ein and ein not in seen_eins and ein not in ALREADY_DONE:
                    seen_eins.add(ein)
                    all_orgs.append(o)
    print(f"  Found {len(all_orgs)} new foundations to process")

    # Step 2: Process each foundation
    processed = 0
    total_board = 0
    for i, org in enumerate(all_orgs):
        name = org.get("name", "Unknown")
        ein = str(org.get("ein", ""))

        # Get latest object_id
        try:
            r = requests.get(PROPUBLICA_ORG.format(ein=ein), headers=HEADERS, timeout=15)
            if r.status_code != 200: continue
            od = r.json().get("organization", {})
            oid = od.get("latest_object_id", "")
            name = od.get("name", name)
        except:
            continue

        if not oid:
            print(f"  [{i+1}/{len(all_orgs)}] ❌ {name} — no filing")
            continue

        board = get_board(oid, name)
        if not board:
            print(f"  [{i+1}/{len(all_orgs)}] ❌ {name} — no board found")
            continue

        # Save foundation itself
        try:
            db.add_relationship(None, name, "NONPROFIT", None, name, "NONPROFIT", "FILER", "IRS_990")
        except: pass

        # Save board members
        for nm, title in board:
            try:
                db.add_relationship(None, nm, "PERSON", None, name, "NONPROFIT", "BOARD_MEMBER", "IRS_990")
                total_board += 1
            except: pass

        processed += 1
        elapsed = time.time() - start
        print(f"  [{i+1}/{len(all_orgs)}] ✅ {name[:50]:50s} — {len(board)} board members ({elapsed:.0f}s)")

        # Rate limit
        time.sleep(0.5)

    elapsed = time.time() - start
    print(f"\nDone: {processed} foundations processed, {total_board} board members added")
    print(f"Time: {elapsed:.0f}s ({elapsed/60:.1f}m)")

if __name__ == "__main__":
    main()
