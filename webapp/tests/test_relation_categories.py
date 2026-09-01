"""Regression tests for relation_categories.py's categorize().

Locks in two real bugs found and fixed 2026-08-29/31, so categorize() can't
silently regress into the same failure mode again:

  - FEC harvester used to embed the dollar amount into relation_type
    ("DONATION ($250)") -- broke exact-match categorization for ~2M rows
    until fixed at the source (fec_contributions.py).
  - IRS_990/IRS_990_TEOS writes non-normalized "POSITION (<role>)" text --
    "POSITION (DIRECTOR)" used to false-match into CO_EXECUTIVE via a
    substring collision ("CTO" inside "di-CTO-r"), instead of falling into
    (or being handled outside) any board-equivalent category. This source's
    board-role parsing was worked around at the query level in
    build_group_rankings.py rather than fixed in categorize() itself -- these
    tests document the current (imperfect but known) behavior so a future
    change to categorize() doesn't silently make it worse without anyone
    noticing.
"""
import sys
from pathlib import Path

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from relation_categories import categorize, validate_relation_type  # noqa: E402


def test_fec_amount_embedded_relation_types_are_not_donation():
    """Documents the bug (fixed at the source, not here): before the fix, FEC
    wrote relation_type as "DONATION ($<amount>)" per row, which never
    exact-matches the plain "DONATION" string categorize() checks for. If a
    harvester regresses to writing amount-embedded strings again, this test
    makes the resulting mis-categorization visible instead of silent."""
    assert categorize("DONATION") == "DONATION"
    assert categorize("DONATION ($250)") != "DONATION"


def test_irs_position_wrapper_does_not_land_in_co_director():
    """"POSITION (DIRECTOR)" is confirmed to NOT categorize as CO_DIRECTOR --
    it false-matches CO_EXECUTIVE via a "CTO" substring collision inside
    "di-CTO-r". This is the known, current (imperfect) behavior -- callers
    that need real board-role classification for IRS-sourced data must parse
    the raw "POSITION (<role>)" string directly (see build_group_rankings.py's
    assemble_board()), not rely on categorize(). This test exists so a future
    edit to categorize() that changes this behavior does so knowingly."""
    assert categorize("DIRECTOR") == "CO_DIRECTOR"
    assert categorize("POSITION (DIRECTOR)") != "CO_DIRECTOR"
    assert categorize("POSITION (DIRECTOR)") == "CO_EXECUTIVE"  # the actual (accidental) result


def test_irs_position_wrapper_casing_variants_are_inconsistent():
    """Different casing/abbreviation variants of the same real-world role
    ("POSITION (DIRECTOR)" vs "POSITION (Dir)") currently land in different
    categorize() buckets -- documents the inconsistency rather than asserting
    a "correct" answer, since there isn't one without fixing the harvester
    output itself (out of scope -- see build_group_rankings.py's module
    docstring for why this was worked around at the query level instead)."""
    assert categorize("POSITION (DIRECTOR)") == "CO_EXECUTIVE"
    assert categorize("POSITION (Dir)") == "OTHER"  # falls through entirely -- no keyword match


def test_clean_board_relation_types_categorize_correctly():
    """Sanity check: relation_type values that ARE clean constants (the
    non-IRS sources: LittleSis/SEC/AmLaw) categorize correctly today -- this
    is what a fixed/normalized relation_type should look like."""
    assert categorize("DIRECTOR") == "CO_DIRECTOR"
    assert categorize("BOARD_MEMBER") == "CO_DIRECTOR"
    assert categorize("CHAIRMAN_OF_THE_BOARD") == "CO_DIRECTOR"
    assert categorize("MEMBER_OF") == "MEMBERSHIP"
    assert categorize("TRUSTEE") == "ADVISORY"


def test_validate_relation_type_flags_the_two_known_bug_patterns():
    """Direct coverage for the new shape-check helper (relation_categories.py)
    that new/touched harvesters are expected to call before INSERT. No
    harvester wires this in yet -- the fleet has no established cross-repo
    import pattern into webapp/ (checked: none currently import it), so
    audit_relation_types.py's retroactive periodic check is the only live
    consumer today."""
    is_clean, reasons = validate_relation_type("DONATION")
    assert is_clean and reasons == []

    is_clean, reasons = validate_relation_type("DONATION ($250)")
    assert not is_clean and "digit" in reasons[0]

    is_clean, reasons = validate_relation_type("POSITION (DIRECTOR)")
    assert not is_clean and any("parenthesized" in r for r in reasons)

    is_clean, reasons = validate_relation_type("")
    assert not is_clean and reasons == ["empty/None"]

    is_clean, reasons = validate_relation_type(None)
    assert not is_clean and reasons == ["empty/None"]


def test_patent_coinventor_source_relation_types_categorize_correctly():
    """Documents a third real bug, found 2026-08-31 via audit_relation_types.py's
    first live run: patent_inventor.py (source_data=PATENT_COINVENTOR, ~1.9M
    rows -- effectively this harvester's entire output) writes "CO_INVENTOR_WITH"
    and "INVENTOR_AT", neither of which exact-matched categorize()'s old
    "CO_INVENTOR"-only / EMPLOYMENT lists -- both silently fell to OTHER
    (score 0.05) instead of CO_INVENTOR (0.68) / EMPLOYMENT (0.70), a >13x
    under-weighting across the whole source. "IDENTITY" is a deliberate
    self-loop (source_name == target_name) the harvester inserts to register
    a low-confidence co-inventor as a graph node -- it's a same-entity marker,
    not a real relationship, so it belongs with ALIAS/FORMER_NAME in
    SAME_ENTITY (build_scored_edges.py already drops that category from
    scoring entirely, which is the correct behavior for a self-loop)."""
    assert categorize("CO_INVENTOR_WITH") == "CO_INVENTOR"
    assert categorize("INVENTOR_AT") == "EMPLOYMENT"
    assert categorize("IDENTITY") == "SAME_ENTITY"


def test_person_reconciliation_cross_referenced_is_same_entity():
    """A fourth instance of the same self-loop pattern, also found via the
    2026-08-31 audit: person_reconciliation.py (source_data=RECONCILIATION,
    43,049 rows -- 100% of that source) writes "CROSS_REFERENCED" with
    source_name == target_name (confirming two QIDs refer to the same person)
    -- a same-entity marker, not a real relationship, same as IDENTITY."""
    assert categorize("CROSS_REFERENCED") == "SAME_ENTITY"


def test_unrecognized_relation_type_falls_through_to_other():
    """categorize() has no "reject unknown input" mode -- anything
    unrecognized silently becomes OTHER. This is the actual mechanism behind
    both bugs this suite documents: there's no signal distinguishing "a
    legitimately novel relation type" from "a malformed one" at this layer.
    audit_relation_types.py (repo root) is the tool that surfaces OTHER-bucket
    volume per source_data so this silence doesn't go unnoticed indefinitely."""
    assert categorize("SOME_TOTALLY_MADE_UP_RELATION_TYPE_XYZ") == "OTHER"
    assert categorize("") == "OTHER"
    assert categorize(None) == "OTHER"
