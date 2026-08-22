"""Build the women's-issues nonprofit directory DB from IRS EO BMF extracts.

Reads data/raw/bmf/eo{1..4}.csv, applies filter_sector from
womens_nonprofits_pipeline (TDD-tested), and writes
data/womens_issues_nonprofits.db with schema + FTS5 via create_schema/insert_org.

Financials from the BMF itself (ASSET_AMT, INCOME_AMT, REVENUE_AMT) populate
total_revenue; ProPublica enrichment is a later phase (rate-limited).
Missions LEFT JOIN from org_missions when the Optiplex sync copy is present.

Usage: python scripts/build_womens_directory.py
"""
import csv
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from womens_nonprofits_pipeline import (
    create_schema, insert_org, filter_sector, compute_efficiency_ratio,
)

BMF_DIR = ROOT / "data" / "raw" / "bmf"
OUT_DB = ROOT / "data" / "womens_issues_nonprofits.db"
# Optiplex-synced pipeline DB (missions), if present
MISSIONS_DB = ROOT / "data" / "pipeline_cache.db"

NTEE_CATEGORY = {
    "P46": "Domestic Violence Shelters & Services",
    "P43": "Family Violence Prevention",
    "E42": "Reproductive Health Care",
    "E22": "Women's Health",
    "R24": "Women's Rights Advocacy",
    "U30": "Women in Physical Sciences / STEM",
    "B40": "Women in Higher Education & Professions",
    "S31": "Vocational Training for Women",
    "O54": "Girls' Youth Development",
    "L20": "Women's Housing",
    "I21": "Anti-Trafficking",
    "P44": "Permanent Supportive Housing",
}


def bmf_row_to_org(row):
    ein_raw = row.get("EIN", "").strip()
    if len(ein_raw) == 9 and ein_raw.isdigit():
        ein = f"{ein_raw[:2]}-{ein_raw[2:]}"
    else:
        ein = ein_raw
    ntee = row.get("NTEE_CD", "").strip()
    prefix = ntee[:3]
    revenue = row.get("REVENUE_AMT", "").strip()
    try:
        revenue = float(revenue) if revenue else None
    except ValueError:
        revenue = None
    return {
        "ein": ein,
        "name": row.get("NAME", "").strip().title(),
        "city": row.get("CITY", "").strip().title(),
        "state": row.get("STATE", "").strip(),
        "ntee_code": ntee,
        "ntee_category": NTEE_CATEGORY.get(prefix, prefix),
        "subsection": "501(c)(3)" if row.get("SUBSECTION", "").strip() == "03" else row.get("SUBSECTION", ""),
        "mission": None,
        "primary_focus": NTEE_CATEGORY.get(prefix),
        "website": None,
        "total_revenue": revenue,
        "program_expenses": None,
        "tax_year": None,
        "efficiency_ratio": None,
    }


def load_missions():
    """EIN -> mission from the pipeline_cache org_missions table, if present."""
    if not MISSIONS_DB.exists():
        return {}
    try:
        c = sqlite3.connect(f"file:{MISSIONS_DB}?mode=ro", uri=True)
        rows = c.execute("SELECT ein, mission FROM org_missions").fetchall()
        c.close()
        return dict(rows)
    except sqlite3.Error:
        return {}


def main():
    missions = load_missions()
    if OUT_DB.exists():
        OUT_DB.unlink()
    conn = sqlite3.connect(OUT_DB)
    create_schema(conn)

    total, kept = 0, 0
    for eo in sorted(BMF_DIR.glob("eo*.csv")):
        with open(eo, encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                total += 1
                ntee = row.get("NTEE_CD", "").strip()
                name = row.get("NAME", "").strip()
                if not filter_sector(ntee, name):
                    continue
                org = bmf_row_to_org(row)
                # mission from harvested org_missions (keyed by EIN)
                m = missions.get(org["ein"]) or missions.get(org["ein"].replace("-", ""))
                if m:
                    org["mission"] = m
                org["efficiency_ratio"] = compute_efficiency_ratio(
                    org["program_expenses"], org["total_revenue"])
                try:
                    insert_org(conn, org)
                    kept += 1
                except sqlite3.IntegrityError:
                    pass  # duplicate EIN across region files

    # validation report
    by_ntee = conn.execute(
        "SELECT ntee_category, COUNT(*) FROM womens_501c3_nonprofits GROUP BY ntee_category ORDER BY 2 DESC"
    ).fetchall()
    with_mission = conn.execute(
        "SELECT COUNT(*) FROM womens_501c3_nonprofits WHERE mission IS NOT NULL").fetchone()[0]
    conn.close()

    print(f"scanned {total:,} BMF rows -> kept {kept:,} orgs")
    print(f"with mission text: {with_mission:,}")
    print("\nBy category:")
    for cat, n in by_ntee:
        print(f"  {n:5,}  {cat}")
    print(f"\nwrote {OUT_DB} ({OUT_DB.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
