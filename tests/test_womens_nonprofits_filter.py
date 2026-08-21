"""TDD (red phase) for specifications/womens-nonprofits-directory.md, filter step.

Defines the expected contract for the not-yet-written `womens_nonprofits_pipeline`
module (repo root):

    NTEE_INCLUDE_PREFIXES: frozenset[str]
        Exact set from the spec's "Include" list:
        P46, P43, E42, E22, R24, U30, B40, S31, O54, L20, W30, I21, P44

    NTEE_EXCLUDE_PREFIXES: frozenset[str]
        {"R40", "R60"} -- voter education/registration/civil-rights-voting,
        per spec's "Exclude" list ("R40, R60-series").

    VOTING_NAME_RE: re.Pattern
        Exactly the spec's regex:
        (?i)\\b(vote|voter|voting|election|ballot|electoral|league of women voters)\\b

    filter_sector(ntee_code: str, name: str) -> bool
        True iff the org should be INCLUDED in the directory:
          - ntee_code starts with one of NTEE_INCLUDE_PREFIXES, AND
          - ntee_code does NOT start with any of NTEE_EXCLUDE_PREFIXES, AND
          - name does NOT match VOTING_NAME_RE.
        Exclusion (NTEE or name) always wins over inclusion (hard reject).

Until womens_nonprofits_pipeline.py exists, every test here fails at
collection (ModuleNotFoundError) -- that is the expected RED state for a
TDD gate (a real failure, not a skip, so a "tests must pass" CI gate can't
pass vacuously while this is unimplemented).
"""
import pytest

from womens_nonprofits_pipeline import (
    NTEE_INCLUDE_PREFIXES,
    NTEE_EXCLUDE_PREFIXES,
    VOTING_NAME_RE,
    filter_sector,
)


# ── NTEE include list, one case per spec code ──
INCLUDE_CASES = [
    ("P46", "Example Domestic Violence Shelter"),
    ("P43", "Example Family Violence Services"),
    ("E42", "Example Reproductive Health Clinic"),
    ("E22", "Example Women's Health Foundation"),
    ("R24", "Example Women's Rights Advocacy Group"),
    ("U30", "Example Women in Physical Sciences Society"),
    ("B40", "Example Women in Higher Education Fund"),
    ("S31", "Example Vocational Training for Women"),
    ("O54", "Example Girls Youth Development Program"),
    ("L20", "Example Women's Housing Trust"),
    ("W30", "Example Women's Microfinance Fund"),
    ("I21", "Example Anti-Trafficking Coalition"),
    ("P44", "Example Permanent Supportive Housing for Women"),
]


@pytest.mark.parametrize("ntee_code,name", INCLUDE_CASES)
def test_include_ntee_codes(ntee_code, name):
    assert filter_sector(ntee_code, name) is True


def test_ntee_prefix_match_allows_subcodes():
    """Spec says 'exact match on prefix' -- a longer code that STARTS WITH an
    include prefix (e.g. a 990-e-file subcode like P4601) should still match."""
    assert filter_sector("P4601", "Example Shelter With Subcode") is True


def test_ntee_prefix_does_not_match_substring_not_at_start():
    """The include code must be a PREFIX, not merely a substring anywhere in
    the ntee_code string."""
    assert filter_sector("XP46", "Example Org") is False


@pytest.mark.parametrize(
    "ntee_code,name",
    [
        ("A20", "Example Arts Council"),  # unrelated sector, not in include list
        ("Q30", "Example International Development Org"),  # NOT in include list
        ("X99", "Example Unclassified Org"),
        ("", "Example Org With No NTEE Code"),
    ],
)
def test_exclude_unlisted_ntee_codes(ntee_code, name):
    assert filter_sector(ntee_code, name) is False


# ── NTEE hard-exclude codes (voting/civil-rights-voting) ──
@pytest.mark.parametrize("ntee_code", ["R40", "R60"])
def test_exclude_voting_ntee_codes(ntee_code):
    assert filter_sector(ntee_code, "Example Org With A Neutral Name") is False


def test_exclude_ntee_wins_even_with_neutral_name():
    """A voting-related NTEE code is a hard reject even when the org name
    itself gives no hint of voting/elections."""
    assert filter_sector("R60", "Community Action Alliance") is False


# ── Name-regex hard-exclude (the "League of Women Voters" edge case) ──
def test_exclude_league_of_women_voters_despite_included_ntee():
    """The exact edge case called out in the spec's TDD Plan: an org whose
    NTEE code (R24, women's rights advocacy) is on the include list must
    still be EXCLUDED because of its name."""
    assert (
        filter_sector("R24", "League of Women Voters of California Education Fund")
        is False
    )


@pytest.mark.parametrize(
    "name",
    [
        "Ohio Vote Project",
        "Get Out The Vote Coalition",
        "Women Voter Education Fund",  # "Voter" as a whole word
        "Register to Vote Now",
        "Election Integrity Project",
        "Ballot Access Coalition",
        "Electoral Reform Institute",
        "League of Women Voters of Texas",
        "VOTE Women Ohio",  # case-insensitivity
    ],
)
def test_exclude_by_name_regex_even_with_included_ntee(name):
    # Use an included NTEE code (R24) so only the name regex can be
    # responsible for exclusion here.
    assert filter_sector("R24", name) is False


@pytest.mark.parametrize(
    "name",
    [
        "Women's Devotion Society",  # contains "votion", not the whole word "vote"
        "Nontraditional Employment for Women (NEW)",
        "Society of Women Engineers",
        "Center for Women and Enterprise",
    ],
)
def test_name_regex_does_not_false_positive_on_substrings(name):
    assert filter_sector("R24", name) is True


def test_voting_name_regex_matches_expected_pattern():
    assert VOTING_NAME_RE.search("League of Women Voters") is not None
    assert VOTING_NAME_RE.search("Voter Registration Drive") is not None
    assert VOTING_NAME_RE.search("Devotion") is None
    assert VOTING_NAME_RE.search("Advocate") is None


def test_ntee_include_and_exclude_sets_match_spec():
    assert NTEE_INCLUDE_PREFIXES == frozenset(
        {"P46", "P43", "E42", "E22", "R24", "U30", "B40", "S31", "O54", "L20", "W30", "I21", "P44"}
    )
    assert NTEE_EXCLUDE_PREFIXES == frozenset({"R40", "R60"})
