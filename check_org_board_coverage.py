"""One-shot diagnostic: for a list of candidate org name variants, report how
many PERSON-sourced board-category rows target each exact name. Reuses the
same categorize()+IRS-whitelist logic as build_group_rankings.py's
assemble_board() so results predict what that script will actually resolve.

Read-only. Uses the (target_type, target_name) index for fast exact lookups
(unlike the earlier ad-hoc diagnostic this session, which omitted
target_type and triggered a slow scan).
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))
from relation_categories import categorize

DB = os.environ.get("DB_PATH", r"C:\Users\johnk\data\pipeline_cache.db")

BOARD_CATEGORIES = {"CO_DIRECTOR"}
FALLBACK_CATEGORIES = {"ADVISORY", "CO_EXECUTIVE"}
_UNINFORMATIVE_RELATION_TYPES = {"POSITION"}
IRS_BOARD_ROLES = {"DIRECTOR", "DIR", "TRUSTEE", "CO-TRUSTEE", "BOARD MEMBER", "CHAIRMAN", "CHAIR"}

CANDIDATES = {
    "Millennium": ["Millennium Management", "Millennium Management LLC", "Millennium Management, LLC"],
    "Point72": ["Point72 Asset Management", "Point72 Asset Management, L.P.", "Point72"],
    "Citadel": ["Citadel LLC", "Citadel Advisors", "Citadel Advisors LLC", "Citadel"],
    "Bank of America": ["Bank of America", "Bank of America Corporation", "BANK OF AMERICA CORP"],
    "JPMorgan Chase": ["JPMorgan Chase", "JPMorgan Chase & Co", "JPMorgan Chase & Co.",
                       "JPMORGAN CHASE & CO", "J P MORGAN CHASE & CO", "JPMorgan Chase Bank, N.A.",
                       "JPMorgan Chase Bank", "JPMorgan", "JP Morgan"],
    "Ford (auto)": ["Ford Motor Company", "FORD MOTOR CO"],
    "General Motors": ["General Motors", "General Motors Company", "GENERAL MOTORS CO"],
    "IBM": ["IBM", "International Business Machines", "INTERNATIONAL BUSINESS MACHINES CORP"],
    "Google": ["Google", "Google LLC", "Alphabet Inc", "Alphabet Inc.", "ALPHABET INC"],
    "Anthropic": ["Anthropic", "Anthropic PBC"],
    "SpaceX": ["SpaceX", "Space Exploration Technologies Corp"],
    "Fox": ["Fox Corporation", "FOX CORP"],
    "Microsoft": ["Microsoft", "Microsoft Corporation", "MICROSOFT CORP"],
    "MacArthur Foundation": ["John D. and Catherine T. MacArthur Foundation", "MacArthur Foundation"],
    "Carnegie Foundation": ["Carnegie Corporation of New York", "Carnegie Foundation"],
    "Gates Foundation": ["Bill and Melinda Gates Foundation", "Bill & Melinda Gates Foundation", "Gates Foundation"],
    "Ford Foundation (existing)": ["Ford Foundation"],
    "Goldman Sachs (existing, known-good)": ["Goldman Sachs"],
}


def count_board_rows(conn, target_org):
    cur = conn.cursor()
    cur.execute(
        "SELECT source_name, relation_type, source_data FROM relationships "
        "WHERE target_name = ? AND target_type = 'ORG' AND source_type = 'PERSON'",
        (target_org,),
    )
    rows = cur.fetchall()
    director_members = set()
    fallback_members = set()
    for source_name, relation_type, source_data in rows:
        if source_data in ("IRS_990_TEOS", "IRS_990"):
            rt = (relation_type or "").upper().strip()
            if rt.startswith("POSITION (") and rt.endswith(")"):
                role = rt[len("POSITION ("):-1].strip()
                if role in IRS_BOARD_ROLES:
                    director_members.add(source_name)
            continue
        cat = categorize(relation_type)
        if cat in BOARD_CATEGORIES:
            director_members.add(source_name)
        elif cat in FALLBACK_CATEGORIES and (relation_type or "").strip().upper() not in _UNINFORMATIVE_RELATION_TYPES:
            fallback_members.add(source_name)
    members = director_members or fallback_members
    tier = "director" if director_members else ("fallback" if fallback_members else "none")
    return len(rows), len(members), tier


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    for label, variants in CANDIDATES.items():
        print(f"\n{label}:")
        for v in variants:
            total_rows, n_members, tier = count_board_rows(conn, v)
            flag = f"  <-- {tier.upper()}" if n_members > 0 else ""
            print(f"    {v!r}: {total_rows} PERSON rows, {n_members} members{flag}")
    conn.close()


if __name__ == "__main__":
    main()
