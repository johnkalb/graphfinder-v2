import json
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\johnk\data\pipeline_cache.db")
SOURCE_TAG = "CALSTRS_PRIVATE_EQUITY_PERFORMANCE_2025"
SOURCE_URL = "https://www.calstrs.com/files/6765c6592/CalSTRSPrivateEquityPerformanceReportFYE2025.pdf"
SOURCE_TITLE = "CalSTRS Private Equity Portfolio Performance as of June 30, 2025"
ALLOCATOR = "CalSTRS"

FUNDS = [
    {
        "name": "Apollo Hybrid Value Fund II LP",
        "page": 1,
        "vintage_year": 2021,
        "committed": "250,000,000",
        "contributed": "240,036,575",
        "distributed": "49,081,757",
        "market_value": "244,189,363",
        "irr": "11.25",
    },
    {
        "name": "Apollo Investment Fund IX, L.P.",
        "page": 1,
        "vintage_year": 2019,
        "committed": "300,000,000",
        "contributed": "300,616,854",
        "distributed": "172,061,478",
        "market_value": "281,789,208",
        "irr": "15.41",
    },
    {
        "name": "Blackstone Capital Partners VIII L.P.",
        "page": 2,
        "vintage_year": 2021,
        "committed": "750,000,000",
        "contributed": "676,719,798",
        "distributed": "140,484,768",
        "market_value": "727,121,980",
        "irr": "10.44",
    },
    {
        "name": "Blackstone Partners IX, L.P.",
        "page": 2,
        "vintage_year": 2025,
        "committed": "500,000,000",
        "contributed": "29,064,177",
        "distributed": "-",
        "market_value": "26,538,019",
        "irr": "(8.69)",
    },
    {
        "name": "Francisco Partners VI, L.P.",
        "page": 4,
        "vintage_year": 2021,
        "committed": "300,000,000",
        "contributed": "291,750,000",
        "distributed": "50,074,269",
        "market_value": "375,862,438",
        "irr": "14.12",
    },
    {
        "name": "Francisco Partners VII A L.P.",
        "page": 4,
        "vintage_year": 2023,
        "committed": "300,000,000",
        "contributed": "93,300,000",
        "distributed": "-",
        "market_value": "99,987,602",
        "irr": "13.71",
    },
    {
        "name": "KKR 2006 Fund L.P.",
        "page": 5,
        "vintage_year": 2007,
        "committed": "300,000,000",
        "contributed": "311,779,643",
        "distributed": "554,108,979",
        "market_value": "-",
        "irr": "8.35",
    },
    {
        "name": "KKR Americas Fund XII L.P.",
        "page": 5,
        "vintage_year": 2017,
        "committed": "300,000,000",
        "contributed": "305,245,111",
        "distributed": "277,764,543",
        "market_value": "381,889,015",
        "irr": "19.40",
    },
    {
        "name": "Thoma Bravo Fund XIV",
        "page": 8,
        "vintage_year": 2021,
        "committed": "300,000,000",
        "contributed": "324,641,678",
        "distributed": "104,097,648",
        "market_value": "300,532,611",
        "irr": "6.83",
    },
    {
        "name": "Thoma Bravo Fund XV, L.P.",
        "page": 8,
        "vintage_year": 2022,
        "committed": "300,000,000",
        "contributed": "258,417,627",
        "distributed": "10,118,485",
        "market_value": "345,210,820",
        "irr": "15.02",
    },
]

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("DELETE FROM relationships WHERE source_data=?", (SOURCE_TAG,))
inserted = 0
for fund in FUNDS:
    evidence = json.dumps({
        "source_title": SOURCE_TITLE,
        "source_url": SOURCE_URL,
        "doc_type": "private_equity_performance_report",
        "allocator": ALLOCATOR,
        "year": 2025,
        "page": fund["page"],
        "vintage_year": fund["vintage_year"],
        "capital_committed": fund["committed"],
        "capital_contributed": fund["contributed"],
        "capital_distributed": fund["distributed"],
        "market_value": fund["market_value"],
        "irr": fund["irr"],
        "confidence": "high",
        "snippet": f"{fund['name']} appears in the CalSTRS private equity performance report with committed capital of {fund['committed']}.",
    }, separators=(",", ":"))
    cur.execute(
        "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "CALSTRS",
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
