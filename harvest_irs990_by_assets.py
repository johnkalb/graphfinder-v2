#!/usr/bin/env python3
"""IRS 990 discovery by asset size -- the broadest-coverage alternative to
harvest_irs_bulk.py's keyword search.

Why: harvest_irs_bulk.py only processes organizations matching an explicit
ProPublica search keyword (default: ["lehmann", "family foundation"]), and
ProPublica's search API requires a real keyword -- there's no "browse all"
mode. That means any org whose name doesn't happen to match a past keyword
is invisible regardless of how notable it is. Confirmed directly
(2026-09-16): Robin Hood Foundation had zero rows in the DB for exactly
this reason, despite Dina Powell McCormick chairing its board.

This script instead sources candidates from the IRS's own Business Master
File (https://www.irs.gov/pub/irs-soi/eo{1-4}.csv) -- a complete,
non-keyword-dependent list of every tax-exempt organization in the US, with
each org's reported total assets. Filtering to a $10M+ asset floor and
processing largest-first gives broad, comprehensive coverage of exactly the
organizations most likely to have board members worth having in this graph
(major foundations, universities, hospital systems, etc.), without ever
needing to guess a name or keyword. At that floor: 61,146 candidates
(measured 2026-09-16) -- too many for one run, so this is cursor-driven and
scheduled like every other large sweep in this project, not a one-shot.

Reuses harvest_irs_bulk.py's search_organizations/get_filing_details/
download_and_parse_xml (the same fixed parser used for the Overbrook/Robin
Hood backfill) rather than duplicating that logic.

Usage:
    python harvest_irs990_by_assets.py --build-candidates   # one-time (or
                                                              # periodic) BMF
                                                              # download + filter
    python harvest_irs990_by_assets.py --limit 25            # one tick, resumes
                                                              # from cursor
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import types

sys.path.insert(0, "/home/john/.hermes" if os.name != "nt" else r"C:\Users\johnk\Desktop\sixdegrees")

# harvest_irs_bulk.py imports src.utils.logger (loguru), which isn't needed
# for the functions this script reuses -- stub it out rather than adding a
# new dependency just to satisfy an unused import.
if "src.utils.logger" not in sys.modules:
    _stub = types.ModuleType("src.utils.logger")
    _stub.setup_custom_logger = lambda name: None
    sys.modules["src.utils.logger"] = _stub

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "harvest_irs_bulk",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "harvest_irs_bulk.py"),
)
_irs_bulk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_irs_bulk)

from src.data import pg_shim
from src.data.pg_shim import connect as _pg_connect
from src.data.db_manager import DBManager
from src.config import DB_PATH

BMF_URLS = [f"https://www.irs.gov/pub/irs-soi/eo{i}.csv" for i in (1, 2, 3, 4)]
ASSET_FLOOR = 10_000_000
# 501(c)(4) social-welfare/advocacy groups and 501(c)(6) trade associations
# matter politically well below $10M -- advocacy groups often spend a lot
# while holding modest assets (2026-10-07). Keyed by BMF SUBSECTION code.
SUBSECTION_FLOORS = {"04": 1_000_000, "06": 1_000_000}
# org_exempt_status keeps every org at or above this, so any harvested org
# (and most orgs other sources mention) can be labelled 501(c)(3)/(4)/...
STATUS_FLOOR = 1_000_000
# ~11% of $1M+ 501(c)(4)s are homeowner/condo/club associations -- boards of
# private residents, no political relevance. Not harvested below ASSET_FLOOR.
HOA_NAME = re.compile(
    r"HOMEOWNER|OWNERS ASS|COMMUNITY ASS|COMMU ASSOC|CONDOMINIUM|\bHOA\b|PROPERTY OWNERS|"
    r"MASTER ASS|TOWNHOME|VILLAGE ASS|LAKE ASS|COUNTRY CLUB|\bPOA\b|MAINTENANCE ASS|"
    r"RESIDENTS ASS|SHORES ASS|ISLAND ASS|APARTMENT OWNERS|\bAOAO\b|CO-OWNERS")
CANDIDATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "irs990_asset_candidates.json")
CURSOR_SOURCE = "irs990_by_assets"
RATE_LIMIT_SEC = 1.0


def save_exempt_status(rows):
    """Upsert (ein, name, subsection, asset_amt) into org_exempt_status --
    which 501(c) paragraph each org is under, joinable to relationships by
    name (this script writes the BMF name as target_name)."""
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS org_exempt_status(
            ein TEXT PRIMARY KEY, name TEXT NOT NULL, subsection TEXT NOT NULL,
            asset_amt BIGINT, updated_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("CREATE INDEX IF NOT EXISTS org_exempt_status_name ON org_exempt_status (lower(name))")
        execute_values(cur, """INSERT INTO org_exempt_status (ein, name, subsection, asset_amt) VALUES %s
            ON CONFLICT (ein) DO UPDATE SET name=EXCLUDED.name, subsection=EXCLUDED.subsection,
            asset_amt=EXCLUDED.asset_amt, updated_at=now()""", rows, page_size=5000)
    conn.close()


def build_candidates():
    """Download the full Business Master File; keep orgs at or above their
    subsection's asset floor, cache to CANDIDATES_PATH, and record every
    org's 501(c) subsection in org_exempt_status.

    The harvest cursor is an index into the candidate list, so a rebuild
    keeps every existing candidate at its position and appends new ones
    (largest-first) after them -- the harvest continues where it was."""
    import requests
    rows = {}
    for url in BMF_URLS:
        print(f"[irs990-assets] downloading {url}...", flush=True)
        r = requests.get(url, timeout=90)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            try:
                asset = int(row.get("ASSET_AMT") or 0)
            except ValueError:
                asset = 0
            if asset >= STATUS_FLOOR:
                rows[row["EIN"]] = {"ein": row["EIN"], "name": row["NAME"].strip(),
                                    "subsection": row["SUBSECTION"], "asset_amt": asset}
        print(f"[irs990-assets]   running total: {len(rows)} orgs >= ${STATUS_FLOOR:,}", flush=True)

    existing = []
    if os.path.exists(CANDIDATES_PATH):
        with open(CANDIDATES_PATH) as f:
            existing = json.load(f)
    known = {c["ein"] for c in existing}
    for c in existing:
        if c["ein"] in rows:
            c["subsection"] = rows[c["ein"]]["subsection"]
    new = sorted((c for c in rows.values() if c["ein"] not in known
                  and c["asset_amt"] >= SUBSECTION_FLOORS.get(c["subsection"], ASSET_FLOOR)
                  and (c["asset_amt"] >= ASSET_FLOOR or not HOA_NAME.search(c["name"]))),
                 key=lambda c: -c["asset_amt"])
    candidates = existing + new

    os.makedirs(os.path.dirname(CANDIDATES_PATH), exist_ok=True)
    with open(CANDIDATES_PATH, "w") as f:
        json.dump(candidates, f)
    print(f"[irs990-assets] saved {len(candidates)} candidates ({len(existing)} kept in place, "
          f"{len(new)} appended) to {CANDIDATES_PATH}")

    if os.environ.get("DATABASE_URL"):
        save_exempt_status([(c["ein"], c["name"], c["subsection"], c["asset_amt"]) for c in rows.values()])
        print(f"[irs990-assets] org_exempt_status: {len(rows)} orgs upserted")
    else:
        print("[irs990-assets] DATABASE_URL not set -- org_exempt_status not written")


def get_cursor(db):
    cur = db.cursor()
    cur.execute("SELECT cursor_value FROM harvest_cursors WHERE source = ?", (CURSOR_SOURCE,))
    row = cur.fetchone()
    if row is not None:
        return int(row[0])
    cur.execute(
        "INSERT OR IGNORE INTO harvest_cursors (source, cursor_key, cursor_value) VALUES (?, ?, ?)",
        (CURSOR_SOURCE, "candidate_idx", 0),
    )
    db.commit()
    return 0


def save_cursor(db, value):
    cur = db.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO harvest_cursors (source, cursor_key, cursor_value) VALUES (?, ?, ?)",
        (CURSOR_SOURCE, "candidate_idx", value),
    )
    db.commit()


def process_one(dbm, ein, name):
    details = _irs_bulk.get_filing_details(ein)
    if not details or not details.get("object_id"):
        return None, "no_filing"
    xml_url = _irs_bulk.GIVINGTUESDAY_XML.format(object_id=details["object_id"])
    people = _irs_bulk.download_and_parse_xml(xml_url, name)
    if not people:
        return None, "no_people"
    added = 0
    for p in people:
        dbm.add_relationship(
            src_id=None, src_name=p["name"], src_type=p["type"],
            tgt_id=None, tgt_name=name, tgt_type="NONPROFIT",
            relation="BOARD_MEMBER" if p["type"] == "PERSON" else "FILER",
            source_data="IRS_990",
        )
        added += 1
    return added, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-candidates", action="store_true", help="(re)download the BMF and rebuild the candidate list")
    ap.add_argument("--limit", type=int, default=25, help="candidates to process this tick")
    args = ap.parse_args()

    if args.build_candidates:
        build_candidates()
        return

    if not os.path.exists(CANDIDATES_PATH):
        print("[irs990-assets] no candidate list yet -- run with --build-candidates first")
        return

    if not pg_shim.IS_POSTGRES:
        print("[irs990-assets] DATABASE_URL not set -- this job is Postgres-only. Aborting.")
        return

    with open(CANDIDATES_PATH) as f:
        candidates = json.load(f)

    db = _pg_connect(DB_PATH)
    dbm = DBManager(DB_PATH)
    idx = get_cursor(db)
    print(f"[irs990-assets] {len(candidates)} total candidates, resuming at idx {idx}")

    if idx >= len(candidates):
        print("[irs990-assets] all candidates processed.")
        return

    batch = candidates[idx: idx + args.limit]
    ok = 0
    for i, c in enumerate(batch):
        print(f"[irs990-assets] [{idx+i+1}/{len(candidates)}] {c['name']} (${c['asset_amt']:,})", flush=True)
        try:
            added, status = process_one(dbm, c["ein"], c["name"])
            if status == "ok":
                ok += 1
                print(f"  wrote {added} relationships")
            else:
                print(f"  {status}")
        except Exception as e:
            print(f"  ERROR: {e}")
        time.sleep(RATE_LIMIT_SEC)

    new_idx = idx + len(batch)
    save_cursor(db, new_idx)
    print(f"\n[irs990-assets] Tick complete: {ok}/{len(batch)} orgs processed. Cursor -> {new_idx}")
    dbm.close()
    db.close()


if __name__ == "__main__":
    main()
