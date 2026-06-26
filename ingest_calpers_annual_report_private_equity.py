import json
import sqlite3
from pathlib import Path

DB = Path(r"C:\Users\johnk\data\pipeline_cache.db")
SOURCE_TAG = "CALPERS_2024_25_ANNUAL_INVESTMENT_REPORT"
SOURCE_URL = "https://www.calpers.ca.gov/documents/annual-investment-report-fy-2025/download?inline"
ALLOCATOR = "CalPERS"

FUNDS = [
    ("Francisco Partners Agility II, L.P.", 305),
    ("Francisco Partners Agility III, L.P.", 305),
    ("Francisco Partners II, L.P.", 305),
    ("Francisco Partners III, L.P.", 305),
    ("Francisco Partners VI, L.P.", 305),
    ("Francisco Partners VII, L.P.", 305),
    ("General Catalyst Group XI - Health Assurance, L.P.", 305),
    ("General Catalyst Group XII - Creation, L.P.", 305),
    ("General Catalyst Group XII - Endurance, L.P.", 305),
    ("General Catalyst Group XII - Health Assurance, L.P.", 305),
    ("General Catalyst Group XII - Ignition, L.P.", 305),
    ("HongShan Capital Expansion Fund I, L.P.", 306),
    ("HongShan Capital Growth Fund VII, L.P.", 306),
    ("HongShan Capital Seed Fund III, L.P.", 306),
    ("HongShan Capital Venture Fund IX, L.P.", 306),
    ("Insight Partners XI, L.P.", 306),
    ("Insight Partners XII Buyout Annex Fund, L.P.", 306),
    ("Insight Partners XII, L.P.", 306),
    ("Insight Partners XIII Growth Buyout Fund, L.P.", 306),
    ("Insight Partners XIII, L.P.", 306),
    ("Silver Lake Partners III, L.P.", 309),
    ("Silver Lake Partners IV, L.P.", 309),
    ("Silver Lake Partners V, L.P.", 309),
    ("Silver Lake Partners VI, L.P.", 309),
    ("Silver Lake Partners VII, L.P.", 309),
    ("Silver Lake Strategic Investors VI, L.P.", 309),
    ("Silver Lake Technology Investors IV, L.P.", 309),
    ("Silver Lake Technology Investors V, L.P.", 309),
    ("Thoma Bravo Europe Fund, L.P.", 310),
    ("Thoma Bravo Fund XIV, L.P.", 310),
    ("Thoma Bravo Fund XV, L.P.", 310),
    ("Thrive Capital Partners IX Growth, L.P.", 310),
    ("Thrive Capital Partners IX, L.P.", 310),
    ("Thrive Capital Partners Opportunity Fund, L.P.", 310),
]

conn = sqlite3.connect(DB)
cur = conn.cursor()
cur.execute("DELETE FROM relationships WHERE source_data=?", (SOURCE_TAG,))
inserted = 0
for fund_name, page in FUNDS:
    evidence = json.dumps({
        'source_title': 'CalPERS 2024-25 Annual Investment Report',
        'source_url': SOURCE_URL,
        'doc_type': 'annual_investment_report',
        'year': 2025,
        'page': page,
        'allocator': ALLOCATOR,
        'confidence': 'high',
        'snippet': f"{fund_name} is listed in the CalPERS 2024-25 Annual Investment Report private equity holdings schedule.",
    }, separators=(',', ':'))
    cur.execute(
        "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            'CALPERS',
            ALLOCATOR,
            'ORG',
            fund_name.upper().replace(' ', '_').replace('.', '').replace(',', ''),
            fund_name,
            'ORG',
            'LP_COMMITMENT',
            SOURCE_TAG,
            evidence,
        )
    )
    inserted += 1
conn.commit()
conn.close()
print({'inserted': inserted, 'source_tag': SOURCE_TAG})
