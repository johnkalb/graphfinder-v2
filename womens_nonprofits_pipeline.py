"""Women's-issues nonprofit directory pipeline (Phase 1, v2).

Implements the contract defined by the TDD test suite (v2, 2026-08-21):
- tests/test_womens_nonprofits_filter.py
- tests/test_womens_nonprofits_schema.py
- tests/test_womens_nonprofits_whitelist.py
- tests/test_womens_nonprofits_efficiency.py

Spec: specifications/womens-nonprofits-directory.md (Filter v2 amendment)

Deterministic only — no LLM-generated content. Mission statements are NULL
until the irs_990 org_missions harvest supplies real ones.
"""
import re
import sqlite3

# NTEE include prefixes (women's-issues), verified against empirical BMF
# composition on 2026-08-21 (v1 list was fabricated by the Gemini source
# artifact: E22=hospitals, U30=research, S31=economic development, etc.)
NTEE_INCLUDE_PREFIXES = frozenset({
    "E42",  # reproductive health care (Planned Parenthood, family planning)
    "P43",  # family violence services
    "P45",  # women's services NEC
    "P46",  # domestic violence shelters & services
    "P47",  # pregnancy centers
    "I70",  # women's service organizations (Soroptimist, Zonta)
    "F42",  # rape crisis / sexual assault services
    "R24",  # women's rights advocacy
    "O54",  # youth development (girls' orgs incl. Girl Scouts chapters)
})

# Hard-exclude NTEE prefixes: voter education/registration, civil-rights-voting
NTEE_EXCLUDE_PREFIXES = frozenset({"R40", "R60"})

# Hard-exclude name regex (word-boundary, case-insensitive)
VOTING_NAME_RE = re.compile(
    r"\b(vote|voter|voting|election|ballot|electoral|league of women voters)\b",
    re.IGNORECASE,
)

# v2 name-based supplement: catches women-serving orgs coded generically.
# Note: `midwif` has no trailing word-boundary so it matches Midwife,
# Midwifery, Midwives (fixes a trailing-\b typo in the v2 spec draft).
WOMENS_NAME_SUPPLEMENT_RE = re.compile(
    r"\b(women|women's|womens|woman|girls?|female|maternal|maternity|"
    r"sorority|soroptimist|zonta|breast cancer|ovarian|doula|midwif|"
    r"ywca|girl scouts|junior league)\b",
    re.IGNORECASE,
)


def filter_sector(ntee_code: str, name: str) -> bool:
    """True iff org belongs in the women's-issues directory.

    Exclusion (NTEE R40/R60 prefix, or voting name regex) always wins over
    either inclusion path (NTEE prefix OR name supplement).
    """
    code = (ntee_code or "").strip()
    nm = name or ""
    if any(code.startswith(p) for p in NTEE_EXCLUDE_PREFIXES):
        return False
    if VOTING_NAME_RE.search(nm):
        return False
    if any(code.startswith(p) for p in NTEE_INCLUDE_PREFIXES):
        return True
    return WOMENS_NAME_SUPPLEMENT_RE.search(nm) is not None


def match_reason(ntee_code: str, name: str) -> "str | None":
    """Which inclusion rule(s) caught the org: "ntee" | "name" | "both" | None."""
    if not filter_sector(ntee_code, name):
        return None
    code = (ntee_code or "").strip()
    nm = name or ""
    via_ntee = any(code.startswith(p) for p in NTEE_INCLUDE_PREFIXES)
    via_name = WOMENS_NAME_SUPPLEMENT_RE.search(nm) is not None
    if via_ntee and via_name:
        return "both"
    return "ntee" if via_ntee else "name"


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
    match_reason TEXT,
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
    "match_reason",
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
    cols = list(_ORG_KEYS)
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
