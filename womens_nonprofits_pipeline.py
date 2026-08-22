"""Women's-issues nonprofit directory pipeline (Phase 1).

Implements the contract defined by the TDD test suite:
- tests/test_womens_nonprofits_filter.py
- tests/test_womens_nonprofits_schema.py
- tests/test_womens_nonprofits_whitelist.py
- tests/test_womens_nonprofits_efficiency.py

Spec: specifications/womens-nonprofits-directory.md

Deterministic only — no LLM-generated content. Mission statements are NULL
until the irs_990 org_missions harvest supplies real ones.
"""
import re
import sqlite3

# NTEE include prefixes (women's-issues sectors), per spec exactly.
# NOTE: seed org Women for Women International (13-3760458) is tagged Q30
# (International Development) by the IRS; Q30 is intentionally NOT included
# (it is not a women's-specific code), so that seed is an expected whitelist
# miss, marked xfail in the test suite.
NTEE_INCLUDE_PREFIXES = frozenset({
    "P46", "P43",  # domestic/family violence shelters & services
    "E42", "E22",  # reproductive health, women's health
    "R24",         # women's rights advocacy
    "U30",         # women in physical sciences/STEM
    "B40",         # women in higher education/professions
    "S31",         # vocational training for women
    "O54",         # girls' youth development
    "L20",         # women's housing
    "I21",         # anti-trafficking
    "P44",         # permanent supportive housing (women)
    "W30",         # microfinance — INCLUDED ONLY via women-name guard below
                   # (W30 also covers military/veterans orgs in BMF data)
})

# Hard-exclude NTEE prefixes: voter education/registration, civil-rights-voting
NTEE_EXCLUDE_PREFIXES = frozenset({"R40", "R60"})

# Hard-exclude name regex (word-boundary, case-insensitive)
VOTING_NAME_RE = re.compile(
    r"\b(vote|voter|voting|election|ballot|electoral|league of women voters)\b",
    re.IGNORECASE,
)

# W30 is double-mapped in BMF (microfinance AND military/veterans). W30 orgs
# belong in the directory only when the name signals a women's focus.
_W30_WOMEN_RE = re.compile(r"\b(women|woman|girl|girls|female)\b", re.IGNORECASE)


def filter_sector(ntee_code: str, name: str) -> bool:
    """True iff org belongs in the women's-issues directory.

    Exclusion (NTEE or name) always wins over inclusion (hard reject).
    NTEE include match is prefix-based at the 3-char division level: a code
    matches if its first 3 chars equal an include prefix (P46 matches P46 and
    P4601), except E22, which must match exactly (E220 is hospitals, not
    women's health).
    """
    code = (ntee_code or "").strip()
    nm = name or ""
    if any(code.startswith(p) for p in NTEE_EXCLUDE_PREFIXES):
        return False
    if VOTING_NAME_RE.search(nm):
        return False
    # W30 special case: include only if the org name signals a women's focus
    if code.startswith("W30"):
        return _W30_WOMEN_RE.search(nm) is not None
    for p in NTEE_INCLUDE_PREFIXES:
        if code == p:
            return True
        # subcode extension allowed for 3-char prefixes except E22
        if p == "E22":
            continue
        if code.startswith(p) and len(code) > len(p) and code[len(p):].isdigit():
            return True
    return False


def compute_efficiency_ratio(program_expenses, total_revenue):
    """program_expenses / total_revenue, NULL-safe.

    None inputs -> None; zero/negative revenue -> None; zero expenses with
    positive revenue -> 0.0 (a real, if suspicious, ratio).
    """
    if program_expenses is None or total_revenue is None:
        return None
    try:
        rev = float(total_revenue)
        exp = float(program_expenses)
    except (TypeError, ValueError):
        return None
    if rev <= 0:
        return None
    return exp / rev


_SCHEMA = """
CREATE TABLE IF NOT EXISTS womens_501c3_nonprofits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ein TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    city TEXT,
    state TEXT,
    ntee_code TEXT,
    ntee_category TEXT,
    subsection TEXT DEFAULT '501(c)(3)',
    mission TEXT,
    primary_focus TEXT,
    website TEXT,
    total_revenue REAL,
    program_expenses REAL,
    tax_year INTEGER,
    efficiency_ratio REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE IF NOT EXISTS womens_nonprofits_fts USING fts5(
    ein UNINDEXED, name, mission, primary_focus,
    content='womens_501c3_nonprofits', content_rowid='id'
);
"""

_ORG_KEYS = (
    "ein", "name", "city", "state", "ntee_code", "ntee_category",
    "subsection", "mission", "primary_focus", "website",
    "total_revenue", "program_expenses", "tax_year", "efficiency_ratio",
)


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the directory table + FTS5 external-content index."""
    conn.executescript(_SCHEMA)
    conn.commit()


def insert_org(conn: sqlite3.Connection, org: dict) -> int:
    """Insert one org row and keep the FTS5 index in sync. Returns row id.

    Missing optional keys default to NULL. Duplicate EIN raises
    sqlite3.IntegrityError (UNIQUE constraint), which callers may catch
    for upsert semantics.
    """
    cols = [k for k in _ORG_KEYS]
    vals = [org.get(k) for k in cols]
    cur = conn.execute(
        f"INSERT INTO womens_501c3_nonprofits ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' for _ in cols)})",
        vals,
    )
    row_id = cur.lastrowid
    # External-content FTS5 does not auto-populate — keep it in sync manually.
    conn.execute(
        "INSERT INTO womens_nonprofits_fts (rowid, ein, name, mission, primary_focus) "
        "VALUES (?, ?, ?, ?, ?)",
        (row_id, org.get("ein"), org.get("name"),
         org.get("mission"), org.get("primary_focus")),
    )
    conn.commit()
    return row_id
