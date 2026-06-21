#!/usr/bin/env python3
"""Extract Overbrook Foundation grantees and their boards, then check cross-connections."""
import os, sys, json, time, requests, xml.etree.ElementTree as ET
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from collections import defaultdict
from src.config import DB_PATH
from src.data.db_manager import DBManager

HEADERS = {"User-Agent": "GraphBuilderAdmin admin@graphbuilder.local"}
PROPUBLICA_SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
XML_TPL = "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{oid}_public.xml"
NS = "http://www.irs.gov/efile"

def get_grantees(oid):
    """Extract unique grantee names from the foundation's 990-PF XML."""
    r = requests.get(XML_TPL.format(oid=oid), headers=HEADERS, timeout=30)
    root = ET.fromstring(r.content)
    grantees = set()
    for grp in root.iter(f"{{{NS}}}GrantOrContributionPdDurYrGrp"):
        for name_el in grp.iter(f"{{{NS}}}BusinessNameLine1Txt"):
            if name_el.text and name_el.text.strip():
                grantees.add(name_el.text.strip())
    return sorted(grantees)

def search_ein(name):
    """Search ProPublica for an org by name, return EIN if found."""
    try:
        r = requests.get(PROPUBLICA_SEARCH, headers=HEADERS,
                         params={"q": name, "limit": 3}, timeout=15)
        if r.status_code == 200:
            orgs = r.json().get("organizations", [])
            for o in orgs:
                oname = o.get("name", "").lower().strip()
                if oname == name.lower().strip():
                    return o.get("ein", "")
            # Fuzzy: return first if name overlap
            for o in orgs:
                oname = o.get("name", "").lower()
                if name.lower()[:10] in oname or oname[:10] in name.lower():
                    return o.get("ein", "")
    except:
        pass
    return ""

def get_board_members(ein):
    """Get board members from a grantee's 990."""
    try:
        r = requests.get(PROPUBLICA_ORG.format(ein=ein), headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return [], ""
        od = r.json().get("organization", {})
        oid = od.get("latest_object_id", "")
        name = od.get("name", "Unknown")
        if not oid:
            return [], name
        
        r2 = requests.get(XML_TPL.format(oid=oid), headers=HEADERS, timeout=30)
        if r2.status_code != 200:
            return [], name
        root = ET.fromstring(r2.content)
        
        members = []
        for grp in root.iter(f"{{{NS}}}BusinessOfficerGrp"):
            nm = ""
            title = ""
            for child in grp.iter():
                ct = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ct == "PersonNm":
                    nm = child.text.strip() if child.text else ""
                elif ct == "PersonTitleTxt":
                    title = child.text.strip() if child.text else ""
            if nm:
                members.append({"name": nm, "role": title or "Officer"})
        return members, name
    except:
        return [], ""

def save_to_graph(db, grantees_data):
    """Save foundation, grants, boards to relationships table."""
    total = 0
    for g in grantees_data:
        gname = g["name"]
        # Foundation → Grantee (grant edge)
        try:
            db.add_relationship(
                src_id=None, src_name="Overbrook Foundation", src_type="NONPROFIT",
                tgt_id=None, tgt_name=gname, tgt_type="NONPROFIT",
                relation="GRANTED_TO", source_data="IRS_990"
            )
            total += 1
        except: pass
        
        # Grantee → Board Members
        for m in g.get("board", []):
            try:
                db.add_relationship(
                    src_id=None, src_name=m["name"], src_type="PERSON",
                    tgt_id=None, tgt_name=gname, tgt_type="NONPROFIT",
                    relation="BOARD_MEMBER", source_data="IRS_990"
                )
                total += 1
            except: pass
    
    return total

def main():
    oid = "202543189349104469"
    db = DBManager(DB_PATH)
    
    print("Step 1: Extracting grantee names from Overbrook Foundation filing...")
    grantees = get_grantees(oid)
    print(f"  Found {len(grantees)} unique grantees")
    
    print("\nStep 2: Looking up each grantee's EIN and board...")
    grantees_data = []
    for i, g in enumerate(grantees):
        ein = search_ein(g)
        if ein:
            board, real_name = get_board_members(ein)
            grantees_data.append({"name": real_name, "original_name": g, "ein": ein, "board": board})
            status = f"{len(board)} board members" if board else "no board found"
        else:
            grantees_data.append({"name": g, "ein": "", "board": []})
            status = "not found in ProPublica"
        print(f"  [{i+1}/{len(grantees)}] {g[:50]:50s} → {status}")
    
    boards_found = sum(1 for g in grantees_data if g["board"])
    total_board_members = sum(len(g["board"]) for g in grantees_data)
    print(f"\n  {boards_found}/{len(grantees)} grantees found with board data")
    print(f"  {total_board_members} total board members extracted")
    
    print("\nStep 3: Saving to graph database...")
    total_saved = save_to_graph(db, grantees_data)
    print(f"  {total_saved} relationships saved")
    
    # Check cross-connections
    print("\nStep 4: Checking cross-connections to existing graph...")
    from src.graph.graph_compiler import GraphCompiler
    compiler = GraphCompiler(db)
    g = compiler.build_graph()
    
    all_board_names = set()
    for gd in grantees_data:
        for m in gd["board"]:
            all_board_names.add(m["name"])
    
    connected = 0
    for name in all_board_names:
        for node in g.nodes():
            label = g.nodes[node].get("label", "")
            if label == name:
                neighbors = list(g.neighbors(node))
                other_neighbors = [n for n in neighbors if "Overbrook" not in str(g.nodes[n].get("label", n))]
                if other_neighbors:
                    connected += 1
                    other_labels = [g.nodes[n].get("label", n) for n in other_neighbors[:3]]
                    print(f"  🔗 {name} connected via: {other_labels}")
                    break
    
    print(f"\n  {connected}/{len(all_board_names)} board members have pre-existing connections to the graph")

if __name__ == "__main__":
    main()
