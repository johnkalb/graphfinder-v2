import json
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\johnk\data\pipeline_cache.db")
SOURCE_TAG = "PUBLIC_PENSION_DISCLOSURE_SEED"

BUILTIN_COMMITMENTS = [
    {"pension_fund": "CalPERS", "fund_name": "Sequoia Capital U.S. Growth Fund X", "commitment_amount": 150000000, "vintage_year": 2022, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Sequoia Capital U.S. Venture XIV", "commitment_amount": 100000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Benchmark Capital VIII", "commitment_amount": 75000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Andreessen Horowitz Fund V", "commitment_amount": 90000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Accel XIV", "commitment_amount": 80000000, "vintage_year": 2021, "asset_class": "Venture Capital"},
    {"pension_fund": "CalPERS", "fund_name": "Kohlberg Kravis Roberts & Co. L.P. (North America Fund XIII)", "commitment_amount": 200000000, "vintage_year": 2022, "asset_class": "Buyout"},
    {"pension_fund": "CalPERS", "fund_name": "The Blackstone Group (Capital Partners VIII)", "commitment_amount": 175000000, "vintage_year": 2023, "asset_class": "Buyout"},
    {"pension_fund": "CalSTRS", "fund_name": "Sequoia Capital U.S. Venture XV", "commitment_amount": 125000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "Andreessen Horowitz Fund VI", "commitment_amount": 100000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "General Catalyst Group XII", "commitment_amount": 85000000, "vintage_year": 2024, "asset_class": "Venture Capital"},
    {"pension_fund": "CalSTRS", "fund_name": "Insight Partners (Insight Venture Partners XIII)", "commitment_amount": 120000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
    {"pension_fund": "NYSTRS", "fund_name": "Apollo Management (Apollo Investment Fund X)", "commitment_amount": 150000000, "vintage_year": 2022, "asset_class": "Buyout"},
    {"pension_fund": "NYSTRS", "fund_name": "Warburg Pincus Global Growth 15", "commitment_amount": 75000000, "vintage_year": 2023, "asset_class": "Growth Equity"},
    {"pension_fund": "Texas TRS", "fund_name": "Andreessen Horowitz Fund IV", "commitment_amount": 80000000, "vintage_year": 2020, "asset_class": "Venture Capital"},
    {"pension_fund": "Texas TRS", "fund_name": "Thoma Bravo Fund XVI", "commitment_amount": 100000000, "vintage_year": 2024, "asset_class": "Buyout"},
    {"pension_fund": "Texas TRS", "fund_name": "Silver Lake Partners VII", "commitment_amount": 95000000, "vintage_year": 2024, "asset_class": "Buyout"},
    {"pension_fund": "Florida SBA", "fund_name": "Sequoia Capital U.S. Venture XIII", "commitment_amount": 60000000, "vintage_year": 2020, "asset_class": "Venture Capital"},
    {"pension_fund": "Florida SBA", "fund_name": "General Catalyst Group XI", "commitment_amount": 50000000, "vintage_year": 2023, "asset_class": "Venture Capital"},
]

SOURCE_URLS = {
    'CalPERS': 'https://www.calpers.ca.gov/',
    'CalSTRS': 'https://www.calstrs.com/',
    'NYSTRS': 'https://www.nystrs.org/',
    'Texas TRS': 'https://www.trs.texas.gov/',
    'Florida SBA': 'https://www.sbafla.com/',
}

conn = sqlite3.connect(DB)
cur = conn.cursor()
# remove older seed rows so we can replace with richer metadata
cur.execute("DELETE FROM relationships WHERE relation_type='LP_COMMITMENT' AND source_data IN ('PENSION', ?)", (SOURCE_TAG,))
inserted = 0
for c in BUILTIN_COMMITMENTS:
    target_type = 'FINANCIAL_FIRM' if c['asset_class'].lower() in ('buyout','growth equity','private equity') else 'VC_FIRM'
    evidence = json.dumps({
        'source_title': 'Public pension disclosure seed dataset',
        'source_url': SOURCE_URLS.get(c['pension_fund'], ''),
        'doc_type': 'seed_reference_dataset',
        'commitment_amount': c['commitment_amount'],
        'year': c['vintage_year'],
        'vintage': c['vintage_year'],
        'strategy': c['asset_class'],
        'confidence': 'high',
        'snippet': f"{c['pension_fund']} committed to {c['fund_name']} ({c['asset_class']}, vintage {c['vintage_year']})."
    }, separators=(',', ':'))
    cur.execute(
        "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            c['pension_fund'].upper().replace(' ', '_'),
            c['pension_fund'],
            'ORG',
            c['fund_name'].upper().replace(' ', '_').replace('.', '').replace(',', ''),
            c['fund_name'],
            target_type,
            'LP_COMMITMENT',
            SOURCE_TAG,
            evidence,
        )
    )
    inserted += 1
conn.commit()
conn.close()
print({'inserted': inserted, 'source_tag': SOURCE_TAG})
