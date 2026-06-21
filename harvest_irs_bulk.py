#!/usr/bin/env python3
"""Bulk IRS 990 foundation search and harvester.
Searches the ProPublica Nonprofit API by keyword, then downloads and parses
Form 990 XML to extract officers, directors, and trustees."""
import os, sys, json, time, requests, xml.etree.ElementTree as ET
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.config import DB_PATH
from src.data.db_manager import DBManager
from src.utils.logger import setup_custom_logger

PROPUBLICA_SEARCH = "https://projects.propublica.org/nonprofits/api/v2/search.json"
PROPUBLICA_ORG = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
GIVINGTUESDAY_XML = "https://gt990datalake-rawdata.s3.amazonaws.com/EfileData/XmlFiles/{object_id}_public.xml"
USER_AGENT = "GraphBuilderAdmin admin@graphbuilder.local"
HEADERS = {"User-Agent": USER_AGENT}

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "data", "irs_progress.json")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "data", "irs_results")
os.makedirs(RESULTS_DIR, exist_ok=True)
os.makedirs(os.path.dirname(PROGRESS_FILE), exist_ok=True)

def save_progress(stage, pct, found, parsed, elapsed):
    with open(PROGRESS_FILE, "w") as f:
        json.dump({
            "stage": stage, "pct": pct, "found": found,
            "parsed": parsed, "elapsed": int(elapsed),
            "timestamp": datetime.now().isoformat(),
        }, f, indent=2)

def search_organizations(keyword, limit=50):
    """Search ProPublica by keyword."""
    results = []
    params = {"q": keyword}
    try:
        r = requests.get(PROPUBLICA_SEARCH, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data.get("organizations", [])
    except Exception as e:
        print(f"Search error: {e}")
    return results[:limit]

def get_filing_details(ein):
    """Get latest filing object_id for an EIN."""
    try:
        r = requests.get(PROPUBLICA_ORG.format(ein=ein), headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        filings = data.get("filings", [])
        if filings:
            latest = filings[0]
            return {
                "name": data.get("name", ""),
                "ein": ein,
                "object_id": latest.get("object_id", ""),
                "tax_prd": latest.get("tax_prd", ""),
                "url": latest.get("url", ""),
            }
    except Exception as e:
        print(f"  Filing details error for {ein}: {e}")
    return None

def download_and_parse_xml(xml_url, org_name):
    """Download Form 990 XML and extract officers/directors."""
    try:
        r = requests.get(xml_url, headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
    except:
        return []

    ns = {"ns": "http://www.irs.gov/efile"}
    people = []

    # Extract from ReturnHeader (filer info)
    for filer in root.iter("{http://www.irs.gov/efile}Filer"):
        for name_elem in filer.iter("{http://www.irs.gov/efile}BusinessName"):
            parts = []
            for tag in ["BusinessNameLine1", "BusinessNameLine2"]:
                el = name_elem.find(f"ns:{tag}", ns)
                if el is not None and el.text:
                    parts.append(el.text.strip())
            if parts:
                org_name_found = " ".join(parts)
                # Add the foundation itself as an ORG node
                people.append({"name": org_name_found, "type": "ORGANIZATION", "role": "Filer"})

    # Extract officers — IRS 990-PF uses BusinessOfficerGrp
    ns = "http://www.irs.gov/efile"
    
    # BusinessOfficerGrp (990-PF)
    for grp in root.iter(f"{{{ns}}}BusinessOfficerGrp"):
        name = ""
        title = ""
        for child in grp.iter():
            ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if ctag == "PersonNm":
                name = child.text.strip() if child.text else ""
            elif ctag == "PersonTitleTxt":
                title = child.text.strip() if child.text else ""
        if name:
            people.append({"name": name, "type": "PERSON", "role": title or "Officer"})
    
    # Also try Form990PartVI (standard 990)
    for partvi in root.iter(f"{{{ns}}}Form990PartVI"):
        for grp in partvi.iter(f"{{{ns}}}PersonnelDataGrp"):
            for child in grp.iter():
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "PersonNm":
                    name = child.text.strip() if child.text else ""
                    if name:
                        people.append({"name": name, "type": "PERSON", "role": "Director/Trustee"})
    
    # General Officer/Director/Trustee elements (used in some 990s)
    for tag_name in ["Officer", "Director", "Trustee", "KeyEmployee"]:
        for el in root.iter(f"{{{ns}}}{tag_name}"):
            name = ""
            title = ""
            for child in el.iter():
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag in ("PersonNm", "NamePerson"):
                    name = child.text.strip() if child.text else ""
                elif ctag in ("TitleTxt", "Title"):
                    title = child.text.strip() if child.text else ""
            if name and name not in [p["name"] for p in people]:
                people.append({"name": name, "type": "PERSON", "role": title or tag_name})
    
    # Also check for BoardMembers, Trustees, etc.
    for grp_tag in ["BoardMemberGrp", "TrusteeGrp"]:
        for grp in root.iter(f"{{{ns}}}{grp_tag}"):
            for child in grp.iter():
                ctag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                if ctag == "PersonNm":
                    name = child.text.strip() if child.text else ""
                    if name and name not in [p["name"] for p in people]:
                        people.append({"name": name, "type": "PERSON", "role": grp_tag.replace("Grp", "")})

    return people

def main():
    keywords = sys.argv[1:] if len(sys.argv) > 1 else ["lehmann", "family foundation"]
    logger = setup_custom_logger("irs_bulk")
    db = DBManager(DB_PATH)
    
    start = time.time()
    all_orgs = []
    
    for kw in keywords:
        orgs = search_organizations(kw, limit=50)
        all_orgs.extend(orgs)
        logger.info(f"Search '{kw}': {len(orgs)} results")
    
    # Deduplicate by EIN
    seen_eins = set()
    unique_orgs = []
    for o in all_orgs:
        ein = o.get("ein", "")
        if ein and ein not in seen_eins:
            seen_eins.add(ein)
            unique_orgs.append(o)
    
    logger.info(f"Total unique orgs to process: {len(unique_orgs)}")
    save_progress("search", 0, len(unique_orgs), 0, time.time() - start)
    
    # Process each org
    relationships_added = 0
    for i, org_entry in enumerate(unique_orgs):
        ein = org_entry.get("ein", "")
        name = org_entry.get("name", "Unknown")
        
        # Get full org details including latest_object_id
        try:
            r = requests.get(PROPUBLICA_ORG.format(ein=ein), headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            org_data = r.json().get("organization", {})
            oid = org_data.get("latest_object_id", "")
            name = org_data.get("name", name)
        except:
            continue
        
        if not oid:
            logger.debug(f"No filing object_id for {name} ({ein})")
            continue
        
        # Download and parse
        xml_url = GIVINGTUESDAY_XML.format(object_id=oid)
        people = download_and_parse_xml(xml_url, name)
        
        if not people:
            logger.debug(f"No people extracted from {name}")
            continue
        
        # Save to relationships
        for p in people:
            try:
                db.add_relationship(
                    src_id=None, src_name=p["name"], src_type=p["type"],
                    tgt_id=None, tgt_name=name, tgt_type="NONPROFIT",
                    relation="BOARD_MEMBER" if p["type"] == "PERSON" else "FILER",
                    source_data="IRS_990"
                )
                relationships_added += 1
            except:
                pass
        
        elapsed = time.time() - start
        pct = round((i + 1) / len(unique_orgs) * 100, 1)
        save_progress("processing", pct, len(unique_orgs), relationships_added, elapsed)
        
        if (i + 1) % 5 == 0:
            logger.info(f"Progress: {i+1}/{len(unique_orgs)} ({pct}%) - {relationships_added} relationships")
    
    elapsed = time.time() - start
    save_progress("complete", 100, len(unique_orgs), relationships_added, elapsed)
    logger.info(f"Done: {relationships_added} relationships added in {elapsed:.0f}s")

if __name__ == "__main__":
    main()
