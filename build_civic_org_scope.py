#!/usr/bin/env python3
"""State-, regional- and national-level civic/business membership orgs --
chambers of commerce, service clubs (Kiwanis, Rotary, Lions...), fraternal
orders (Elks, Moose, Knights of Columbus, Masons...) and veterans' groups
(VFW, American Legion) -- whose IRS 990 officers enter the graph even when
they aren't already in it. Local clubs and community chambers stay out
(user decision, 2026-10-07: "stay at the state level or regional level,
not individual communities").

Source: the IRS Business Master File (eo1-4.csv). Level comes from the name
plus the BMF AFFILIATION code (9 = subordinate/local chapter, always out).
"Greater X" and "X Area" chambers count as local; "Regional", "Metro",
"District N" and statewide names count as regional/state.

Writes Postgres table civic_org_scope (ein, name, kind, level, state, subsection);
irs_990.py (TEOS) loads its EINs and skips the known-person gate for them.

Usage:
    python build_civic_org_scope.py            # rebuild the table
    python build_civic_org_scope.py --dry-run  # counts + samples only
"""
import argparse
import csv
import io
import os
import re
import sys
from collections import Counter

import requests

BMF_URLS = [f"https://www.irs.gov/pub/irs-soi/eo{i}.csv" for i in (1, 2, 3, 4)]

KINDS = [
    ("chamber", re.compile(r"\bCHAMBER OF COMMERCE\b|\bCHAMBER OF COM\b|\bCHAMBER\b(?!.*(ORCHESTRA|MUSIC|SINGERS|PLAYERS|ENSEMBLE|OPERA|SYMPHONY|CHORALE|CHORUS|STRINGS|THEATRE|THEATER))")),
    ("kiwanis", re.compile(r"\bKIWANIS\b")),
    ("rotary", re.compile(r"\bROTARY\b")),
    ("lions", re.compile(r"\bLIONS CLUBS?\b|\bLIONS (DISTRICT|MULTIPLE DISTRICT|INTERNATIONAL)\b|\bASSOCIATION OF LIONS\b")),
    ("optimist", re.compile(r"\bOPTIMIST (INTERNATIONAL|CLUB|DISTRICT)\b")),
    ("elks", re.compile(r"\bELKS\b|BENEVOLENT (AND|&) PROTECTIVE ORDER")),
    ("moose", re.compile(r"\bORDER OF (THE )?MOOSE\b|\bMOOSE (LODGE|ASSOCIATION)\b|\bWOMEN OF THE MOOSE\b")),
    ("eagles", re.compile(r"FRATERNAL ORDER OF EAGLES")),
    ("knights_of_columbus", re.compile(r"KNIGHTS OF COLUMBUS")),
    ("masons", re.compile(r"\bGRAND LODGE\b|\bMASONIC\b|(?<!CEMENT )(?<!STONE )(?<!BRICK )\bMASONS\b(?!.*\b(APPRENTICE|APPRNTCSHP|EMPLOYERS|EMPLYRS|PENSION|WELFARE|LOCAL|UNION)\b)|\bGRAND CHAPTER\b.*\bROYAL ARCH\b|\bSHRINERS\b")),
    ("odd_fellows", re.compile(r"\bODD FELLOWS\b|\bI ?O ?O ?F\b")),
    ("vfw", re.compile(r"VETERANS OF FOREIGN WARS|\bVFW\b")),
    ("american_legion", re.compile(r"\bAMERICAN LEGION\b")),
    ("amvets", re.compile(r"\bAMVETS\b")),
    ("dav", re.compile(r"DISABLED AMERICAN VETERANS")),
]

STATES = ["ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO", "CONNECTICUT", "DELAWARE",
          "FLORIDA", "GEORGIA", "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY",
          "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA", "MISSISSIPPI",
          "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO",
          "NEW YORK", "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA",
          "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
          "VIRGINIA", "WASHINGTON", "WEST VIRGINIA", "WISCONSIN", "WYOMING", "DISTRICT OF COLUMBIA",
          "PUERTO RICO"]
_ST = "|".join(sorted(STATES, key=len, reverse=True))
# A state name used as the org's scope -- not a city ("Kansas City", "New
# York City", "Oklahoma City", "Virginia Beach"), county or street.
STATE_SCOPE = re.compile(
    rf"(^(THE )?({_ST})\b(?! (CITY|BEACH|FALLS|SPRINGS|RAPIDS|AVE|AVENUE|STREET|ST|COUNTY|PARK|HEIGHTS)\b)"
    rf"|\b(OF|DEPT|DEPARTMENT|STATE OF) (THE )?({_ST})\b(?! (CITY|BEACH|FALLS|SPRINGS|RAPIDS|AVE|AVENUE|STREET|ST|COUNTY)\b)"
    rf"|\b({_ST}) (STATE|STATEWIDE)\b)")
REGIONAL = re.compile(r"\bREGIONAL\b|\bREGION\b|\bMETRO\b|\bMETROPOLITAN\b|\bDISTRICT( NO)? ?\d|\bMULTIPLE DISTRICT\b|\bSTATEWIDE\b")
NATIONAL = re.compile(r"\bINTERNATIONAL\b|\bOF THE (UNITED STATES|USA|U S A|WORLD)\b|\bUNITED STATES\b|\bOF AMERICA\b|\bNATIONAL\b|\bSUPREME (LODGE|COUNCIL)\b")
# A local club named after its parent ("ROTARY INTERNATIONAL CARROLL ROTARY CLUB").
LOCAL_CLUB = re.compile(r"\b(ROTARY|KIWANIS|LIONS|OPTIMIST|EXCHANGE|CIVITAN) CLUB\b(?!S)")
# Post/lodge/club numbers mark local units; district numbers don't.
_DISTRICT_NO = re.compile(r"\bDISTRICT( NO)? ?[\w-]+|\b\d+(ST|ND|RD|TH) DISTRICT\b|\bMULTIPLE DISTRICT [\w-]+")
LOCAL = re.compile(r"\bGREATER\b|\bAREA\b|\bPOST\b|\bLODGE( NO)? ?\d|\bCLUB OF\b|\bCOUNCIL( NO)? ?\d|\bCHAPTER\b|\bAUXILIARY UNIT\b|\bASSEMBLY( NO)? ?\d")


def classify(name, affiliation):
    """-> (kind, level) or None. level: national | state | regional."""
    n = name.upper()
    kind = next((k for k, p in KINDS if p.search(n)), None)
    if kind is None or affiliation == "9":
        return None
    if LOCAL_CLUB.search(n) or re.search(r"\d", _DISTRICT_NO.sub("", n)):
        return None
    # Posts and their auxiliaries are named after a place or a person
    # ("...OF THE UNITED STATES AUXILIARY EAGLE BEND", "...AUXILIARY TO CARL
    # KUMMERLE", "...DEPT OF MARYLAND JACKSON-JOHNSON MEMORIAL").
    if (re.search(r"\bAUXILIARY TO (?!THE |VETERANS|VFW|DEPT|DEPARTMENT|DISTRICT)", n)
            or re.search(r"\bAUXILIARY (?!TO\b|DEPT|DEPARTMENT|DISTRICT|OF\b|INC\b|$)[A-Z]", n)
            or re.search(r"\bMEMORIAL\b", n)):
        return None
    # "...CHAMBER OF COMMERCE OF LAUREL", "LIONS CLUBS INTERNATIONAL OF
    # ELIZABETHTON TENNESSEE": "of <place>" scopes it to that place.
    m = re.search(r"\b(?:COMMERCE|INTERNATIONAL|CLUBS?|CHAMBER) OF (?:THE )?(.+)$", n)
    if m and not re.match(rf"({_ST}|AMERICA|UNITED STATES|THE UNITED STATES|U ?S\b|USA|COMMERCE|METRO|GREATER)", m.group(1)):
        return None
    # "INTERNATIONAL" alone doesn't make a chamber national ("JFK
    # International Airport Chamber of Commerce").
    if affiliation == "6" or (NATIONAL.search(n) and not (
            kind == "chamber" and not re.search(r"UNITED STATES|NATIONAL|OF AMERICA", n))):
        return kind, "national"
    if STATE_SCOPE.search(n) and not LOCAL.search(n):
        return kind, "state"
    if REGIONAL.search(n) and not LOCAL.search(_DISTRICT_NO.sub("", n)):
        return kind, "regional"
    return None


def build():
    rows = []
    for url in BMF_URLS:
        print(f"[civic-scope] downloading {url}...", flush=True)
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            c = classify(row["NAME"], row["AFFILIATION"])
            if c:
                rows.append((row["EIN"], row["NAME"].strip(), c[0], c[1], row["STATE"], row["SUBSECTION"]))
    return rows


def save(rows):
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    with conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS civic_org_scope(
            ein TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, level TEXT NOT NULL,
            state TEXT, subsection TEXT, updated_at TIMESTAMPTZ DEFAULT now())""")
        cur.execute("TRUNCATE civic_org_scope")
        execute_values(cur, "INSERT INTO civic_org_scope (ein, name, kind, level, state, subsection) VALUES %s "
                            "ON CONFLICT (ein) DO NOTHING", rows, page_size=5000)
    conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rows = build()
    print(f"[civic-scope] {len(rows)} orgs")
    print("  by level:", Counter(r[3] for r in rows).most_common())
    print("  by kind:", Counter(r[2] for r in rows).most_common())
    if args.dry_run:
        import random
        random.seed(1)
        for level in ("national", "state", "regional"):
            sel = [r[1] for r in rows if r[3] == level]
            print(f"  sample {level}:", random.sample(sel, min(25, len(sel))))
        return
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL not set")
    save(rows)
    print("[civic-scope] civic_org_scope written")


if __name__ == "__main__":
    main()
