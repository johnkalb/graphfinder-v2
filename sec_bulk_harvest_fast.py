#!/usr/bin/env python3
"""Bulk SEC Form 4 harvest - process all 10K+ companies using reliable .txt URLs."""
import os, sys, json, time, requests, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.config import DB_PATH
from src.data.db_manager import DBManager
from src.data.form4_client import Form4Client
from collections import defaultdict

headers = {"User-Agent": "GraphBuilderAdmin admin@graphbuilder.local"}

print("=== SEC Bulk Form 4 Harvest ===")

# Step 1: Load all CIKs from company_tickers.json
r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=headers, timeout=30)
cik_data = r.json()
all_ciks = []
for entry in cik_data.values():
    cik = str(entry["cik_str"]).zfill(10)
    ticker = entry.get("ticker", "")
    name = entry.get("name", "")
    all_ciks.append((cik, ticker, name))

print(f"Total companies: {len(all_ciks)}")

# Step 2: Download filing metadata for all CIKs and process Form 4s
db = DBManager(DB_PATH)
client = Form4Client(db)

total_relationships = 0
companies_with_form4 = 0

start_time = time.time()
for i, (cik, ticker, name) in enumerate(all_ciks):
    # Fetch filing metadata (rate-limited to ~8/sec)
    time.sleep(0.12)
    try:
        r2 = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers, timeout=15)
        if r2.status_code != 200:
            continue
        data = r2.json()
        db.save_sec_cache(cik, data)
        
        # Find Form 4 filings
        filings = data.get("filings", {}).get("recent", {})
        forms = filings.get("form", [])
        accs = filings.get("accessionNumber", [])
        
        form4_count = 0
        for j, form in enumerate(forms):
            if form == "4":
                form4_count += 1
                if form4_count <= 3:  # Process 3 most recent
                    acc = accs[j]
                    acc_clean = acc.replace("-", "")
                    acc_dashed = acc
                    txt_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_clean}/{acc_dashed}.txt"
                    
                    # Rate limit for .txt fetch
                    time.sleep(0.3)
                    try:
                        r3 = requests.get(txt_url, headers=headers, timeout=15)
                        if r3.status_code == 200:
                            xml_match = re.search(r"<XML>(.*?)</XML>", r3.text, re.DOTALL)
                            if xml_match:
                                raw_xml = xml_match.group(1).strip()
                                raw_xml = re.sub(r"<\?xml[^>]*\?>", "", raw_xml).strip()
                                xml_content = "<ownershipDocument>" + raw_xml + "</ownershipDocument>"
                                
                                parsed = client.parse_raw_xml(xml_content)
                                for entry in parsed:
                                    issuer_cik = entry["issuer_cik"].strip().zfill(10) if entry["issuer_cik"] else None
                                    if entry["is_director"]:
                                        rel = "DIRECTOR"
                                    elif entry["is_officer"]:
                                        rel = "OFFICER"
                                    else:
                                        rel = "INSIDER"
                                    if entry.get("officer_title"):
                                        rel = f"{rel} ({entry['officer_title']})"
                                    
                                    db.add_relationship(
                                        None, entry["owner_name"], "PERSON",
                                        issuer_cik, entry["issuer_name"], "COMPANY",
                                        rel, "SEC_FORM_4"
                                    )
                                    total_relationships += 1
                    except:
                        pass
        
        if form4_count > 0:
            companies_with_form4 += 1
    except requests.exceptions.ReadTimeout:
        time.sleep(2)
    except:
        pass
    
    if (i + 1) % 100 == 0:
        elapsed = time.time() - start_time
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        print(f"  [{i+1}/{len(all_ciks)}] {rate:.0f}/sec - {companies_with_form4} companies w/ Form 4 - {total_relationships} relationships")

elapsed = time.time() - start_time
print(f"\n=== Complete ===")
print(f"Time: {elapsed:.0f}s ({len(all_ciks)/elapsed:.0f}/sec)")
print(f"Companies with Form 4 filings: {companies_with_form4}")
print(f"Total relationships added: {total_relationships}")
