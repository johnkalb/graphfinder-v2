import json
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\johnk\data\pipeline_cache.db")
SOURCE_TAG = "NYSCRF_MONTHLY_TRANSACTIONS_2026"
ALLOCATOR = "New York State Common Retirement Fund"
SOURCE_TITLE = "New York State Common Retirement Fund Monthly Transaction Reports (2026)"

FUNDS = [
    {
        "name": "Sage Equity Investors, L.P.",
        "manager": "Leonard Green & Partners",
        "amount": "$150 million",
        "month": "January 2026",
        "page": 2,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/january-2026.pdf",
        "snippet": "Sage Equity Investors, L.P. - Leonard Green & Partners - $150 million",
    },
    {
        "name": "Sage Equity Investors-A, L.P.",
        "manager": "Leonard Green & Partners",
        "amount": "$150 million",
        "month": "January 2026",
        "page": 2,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/january-2026.pdf",
        "snippet": "Sage Equity Investors-A, L.P. - Leonard Green & Partners - $150 million",
    },
    {
        "name": "Oceans Ventures Fund III, L.P.",
        "manager": "Oceans Ventures Management",
        "amount": "$25 million",
        "month": "February 2026",
        "page": 1,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/february-2026.pdf",
        "snippet": "Oceans Ventures Fund III, L.P. – Oceans Ventures Management – $25 million",
    },
    {
        "name": "CVC Catalyst III (A), L.P.",
        "manager": "CVC Capital Partners",
        "amount": "$150 million",
        "month": "March 2026",
        "page": 1,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/march-2026.pdf",
        "snippet": "CVC Catalyst III (A), L.P. – CVC Capital Partners – $150 million",
    },
    {
        "name": "Knickerpoint Co-Investment Partners, L.P.",
        "manager": "CVC Capital Partners",
        "amount": "$75 million",
        "month": "March 2026",
        "page": 1,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/march-2026.pdf",
        "snippet": "Knickerpoint Co-Investment Partners, L.P. – CVC Capital Partners – $75 million",
    },
    {
        "name": "TB Project Ledger, L.P.",
        "manager": "TowerBrook Capital Partners",
        "amount": "$18.9 million",
        "month": "March 2026",
        "page": 2,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/march-2026.pdf",
        "snippet": "TB Project Ledger, L.P. – TowerBrook Capital Partners – $18.9 million",
    },
    {
        "name": "Main Capital IX Feeder (A) C.V.",
        "manager": "Main Capital Partners",
        "amount": "€150 million",
        "month": "April 2026",
        "page": 1,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/april-2026.pdf",
        "snippet": "Main Capital IX Feeder (A) C.V. – Main Capital Partners – €150 million",
    },
    {
        "name": "Main Foundation III Feeder (A) C.V.",
        "manager": "Main Capital Partners",
        "amount": "€50 million",
        "month": "April 2026",
        "page": 1,
        "url": "https://www.osc.ny.gov/files/common-retirement-fund/pdf/april-2026.pdf",
        "snippet": "Main Foundation III Feeder (A) C.V. – Main Capital Partners – €50 million",
    },
]

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("DELETE FROM relationships WHERE source_data=?", (SOURCE_TAG,))
inserted = 0
for fund in FUNDS:
    evidence = json.dumps({
        "source_title": SOURCE_TITLE,
        "source_url": fund["url"],
        "doc_type": "monthly_transaction_report",
        "allocator": ALLOCATOR,
        "manager": fund["manager"],
        "month": fund["month"],
        "page": fund["page"],
        "commitment_amount": fund["amount"],
        "confidence": "high",
        "snippet": fund["snippet"],
    }, separators=(",", ":"))
    cur.execute(
        "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "NYSCRF",
            ALLOCATOR,
            "ORG",
            fund["name"].upper().replace(" ", "_").replace(".", "").replace(",", ""),
            fund["name"],
            "ORG",
            "LP_COMMITMENT",
            SOURCE_TAG,
            evidence,
        ),
    )
    inserted += 1
conn.commit()
conn.close()
print({"inserted": inserted, "source_tag": SOURCE_TAG})
