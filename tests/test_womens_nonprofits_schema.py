"""TDD (red phase) for specifications/womens-nonprofits-directory.md, schema step.

Expected contract for `womens_nonprofits_pipeline` (repo root):

    create_schema(conn: sqlite3.Connection) -> None
        Creates womens_501c3_nonprofits and the womens_nonprofits_fts FTS5
        virtual table, exactly as specified in the spec's Output section.

    insert_org(conn: sqlite3.Connection, org: dict) -> int
        Inserts one row into womens_501c3_nonprofits AND keeps
        womens_nonprofits_fts in sync (external-content FTS5 tables do not
        auto-populate from writes to the content table -- the insert helper
        must do both). Returns the new row's id. `org` keys match the table
        columns (ein, name, city, state, ntee_code, ntee_category,
        subsection, mission, primary_focus, website, total_revenue,
        program_expenses, tax_year, efficiency_ratio); missing optional keys
        default to NULL.

Every test here fails at collection (ModuleNotFoundError) until the module
exists -- expected RED state.
"""
import sqlite3

import pytest

from womens_nonprofits_pipeline import create_schema, insert_org


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    create_schema(c)
    yield c
    c.close()


SAMPLE_ORG = {
    "ein": "13-1656041",
    "name": "Girl Scouts of the USA",
    "city": "New York",
    "state": "NY",
    "ntee_code": "O54",
    "ntee_category": "Girls Scouting & Leadership",
    "subsection": "501(c)(3)",
    "mission": "Girl Scouting builds girls of courage, confidence, and character.",
    "primary_focus": "Girl Scout Troops & Character Building",
    "website": "https://girlscouts.org",
    "total_revenue": 118000000.0,
    "program_expenses": 104000000.0,
    "tax_year": 2024,
    "efficiency_ratio": 0.8814,
}


def test_create_schema_creates_main_table(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='womens_501c3_nonprofits'"
    )
    assert cur.fetchone() is not None


def test_create_schema_creates_fts_table(conn):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='womens_nonprofits_fts'"
    )
    assert cur.fetchone() is not None


def test_create_schema_is_idempotent():
    """Calling create_schema twice on the same connection must not raise
    (e.g. use CREATE TABLE IF NOT EXISTS)."""
    c = sqlite3.connect(":memory:")
    create_schema(c)
    create_schema(c)  # should not raise
    c.close()


def test_insert_org_round_trips(conn):
    org_id = insert_org(conn, SAMPLE_ORG)
    row = conn.execute(
        "SELECT ein, name, ntee_code, total_revenue FROM womens_501c3_nonprofits WHERE id=?",
        (org_id,),
    ).fetchone()
    assert row == ("13-1656041", "Girl Scouts of the USA", "O54", 118000000.0)


def test_ein_uniqueness_enforced(conn):
    insert_org(conn, SAMPLE_ORG)
    with pytest.raises(sqlite3.IntegrityError):
        insert_org(conn, SAMPLE_ORG)  # same EIN again


def test_ein_uniqueness_allows_different_eins(conn):
    insert_org(conn, SAMPLE_ORG)
    other = dict(SAMPLE_ORG, ein="52-1845620", name="Rape Abuse and Incest National Network (RAINN)")
    other_id = insert_org(conn, other)
    assert other_id is not None


def test_missing_optional_fields_default_null(conn):
    minimal = {
        "ein": "99-9999999",
        "name": "Minimal Org",
        "ntee_code": "P46",
    }
    org_id = insert_org(conn, minimal)
    row = conn.execute(
        "SELECT mission, website, total_revenue FROM womens_501c3_nonprofits WHERE id=?",
        (org_id,),
    ).fetchone()
    assert row == (None, None, None)


def test_fts5_search_finds_inserted_org_by_mission_text(conn):
    insert_org(conn, SAMPLE_ORG)
    rows = conn.execute(
        "SELECT ein FROM womens_nonprofits_fts WHERE womens_nonprofits_fts MATCH 'courage'"
    ).fetchall()
    assert ("13-1656041",) in rows


def test_fts5_search_finds_inserted_org_by_name(conn):
    insert_org(conn, SAMPLE_ORG)
    rows = conn.execute(
        "SELECT ein FROM womens_nonprofits_fts WHERE womens_nonprofits_fts MATCH 'Scouts'"
    ).fetchall()
    assert ("13-1656041",) in rows


def test_fts5_search_no_match_returns_empty(conn):
    insert_org(conn, SAMPLE_ORG)
    rows = conn.execute(
        "SELECT ein FROM womens_nonprofits_fts WHERE womens_nonprofits_fts MATCH 'zzznotpresentzzz'"
    ).fetchall()
    assert rows == []
