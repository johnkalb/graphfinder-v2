"""Phase 2: ProPublica Nonprofit Explorer financial enrichment.

For each org in data/womens_issues_nonprofits.db, fetch latest filing's
total revenue + functional (program) expenses from ProPublica API and
populate total_revenue, program_expenses, tax_year, efficiency_ratio.

Rate-limited (1 req/sec by default), cached resume: skips EINs already
enriched, so re-runs are cheap. ProPublica API: projects.propublica.org
/nonprofits/api/v2/organizations/{ein}.json — no key required.

Usage: python scripts/enrich_financials.py [--limit N] [--delay 0.5]
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "womens_issues_nonprofits.db"

API = "https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json"
HEADERS = {"User-Agent": "sixdegrees-directory/1.0"}


def fetch_ein(ein_digits):
    """Return (revenue, program_expenses, tax_year) or None on failure."""
    url = API.format(ein=ein_digits)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode())
    except Exception:
        return None
    # v2 API splits filings into with_data / without_data buckets
    filings = d.get("filings_with_data") or d.get("filings") or []
    if not filings:
        return None
    latest = filings[0]
    revenue = latest.get("totrevenue")
    program = (
        latest.get("totfuncexpns")
        or latest.get("totfuncexp")
        or latest.get("functotexp")
    )
    year = latest.get("tax_prd_yr") or latest.get("taxyear") or latest.get("tax_prd")
    try:
        revenue = float(revenue) if revenue is not None else None
        program = float(program) if program is not None else None
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        return None
    return revenue, program, year


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="max EINs to process")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between requests")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    cur = conn.execute(
        "SELECT id, ein FROM womens_501c3_nonprofits WHERE program_expenses IS NULL "
        "ORDER BY total_revenue DESC"
    )
    rows = cur.fetchall()
    if args.limit:
        rows = rows[:args.limit]
    print(f"{len(rows):,} EINs to enrich (highest revenue first)")

    enriched = misses = 0
    for i, (row_id, ein) in enumerate(rows):
        digits = ein.replace("-", "")
        res = fetch_ein(digits)
        if res:
            revenue, program, year = res
            ratio = program / revenue if (revenue and program is not None and revenue > 0) else None
            conn.execute(
                "UPDATE womens_501c3_nonprofits SET total_revenue=?, program_expenses=?, "
                "tax_year=?, efficiency_ratio=? WHERE id=?",
                (revenue, program, year, ratio, row_id),
            )
            enriched += 1
        else:
            misses += 1
        if (enriched + misses) % 100 == 0:
            conn.commit()
            print(f"  {i+1}/{len(rows)} enriched={enriched} misses={misses}")
        time.sleep(args.delay)
    conn.commit()
    conn.close()
    print(f"done: {enriched:,} enriched, {misses:,} no-source")


if __name__ == "__main__":
    main()
