"""TDD (red phase) for specifications/womens-nonprofits-directory.md, filter
step -- v2 (2026-08-21 amendment: "Filter v2", "TDD v2").

v2 replaced the original NTEE include list (empirical BMF sampling showed it
was largely fabricated by the source Gemini artifact -- E22=hospitals,
U30=research institutes, S31=economic development, L20=housing generally,
I21=youth centers, none of which are women-specific) and added a
name-based supplement so generically-coded women-serving orgs still match.

Expected contract for the not-yet-written `womens_nonprofits_pipeline`
module (repo root):

    NTEE_INCLUDE_PREFIXES: frozenset[str]
        v2 list: E42, P43, P45, P46, P47, I70, F42, R24, O54

    NTEE_EXCLUDE_PREFIXES: frozenset[str]
        Unchanged from v1: {"R40", "R60"}

    VOTING_NAME_RE: re.Pattern
        Unchanged from v1:
        \\b(vote|voter|voting|election|ballot|electoral|league of women voters)\\b

    WOMENS_NAME_SUPPLEMENT_RE: re.Pattern
        New in v2, exactly as specified:
        \\b(women|women's|womens|woman|girls?|female|maternal|maternity|
           sorority|soroptimist|zonta|breast cancer|ovarian|doula|midwif|
           ywca|girl scouts|junior league)\\b

    filter_sector(ntee_code: str, name: str) -> bool
        True iff INCLUDED:
          - NOT excluded by NTEE (R40/R60 prefix), AND
          - NOT excluded by VOTING_NAME_RE, AND
          - (ntee_code starts with an include prefix) OR
            (name matches WOMENS_NAME_SUPPLEMENT_RE)
        Exclusion (NTEE or voting name) always wins over either inclusion path.

    match_reason(ntee_code: str, name: str) -> str | None
        New in v2 (backs the Output v2 `match_reason` column):
        "ntee" | "name" | "both" | None (not included).

Until womens_nonprofits_pipeline.py exists, every test here fails at
collection (ModuleNotFoundError) -- expected RED state.

Known spec-drafting gap noted but NOT tested here (not asked for, and
encoding it as a passing assertion would just enshrine the bug): the
supplement regex's `midwif` alternative has a trailing `\\b`, so it does NOT
match "Midwife"/"Midwifery"/"Midwives" (the word boundary fails immediately
after "midwif" because "e"/"e"/"v" all continue as word characters). An org
like "Midwifery Alliance of America" would not be name-matched despite being
squarely in scope. Worth a look before Phase 2, not fixed here.
"""
import pytest

from womens_nonprofits_pipeline import (
    NTEE_INCLUDE_PREFIXES,
    NTEE_EXCLUDE_PREFIXES,
    VOTING_NAME_RE,
    WOMENS_NAME_SUPPLEMENT_RE,
    filter_sector,
    match_reason,
)


def test_ntee_include_and_exclude_sets_match_v2_spec():
    assert NTEE_INCLUDE_PREFIXES == frozenset(
        {"E42", "P43", "P45", "P46", "P47", "I70", "F42", "R24", "O54"}
    )
    assert NTEE_EXCLUDE_PREFIXES == frozenset({"R40", "R60"})


# ── v2 NTEE include list, one neutral-name case per code ──
INCLUDE_CASES = [
    ("E42", "Example Reproductive Health Center"),
    ("P43", "Example Family Violence Services"),
    ("P45", "Example Services Organization"),
    ("P46", "Example Domestic Violence Shelter"),
    ("P47", "Example Pregnancy Center"),
    ("I70", "Example Service Club"),
    ("F42", "Example Crisis Center"),
    ("R24", "Example Rights Advocacy Group"),
    ("O54", "Example Youth Development Program"),
]


@pytest.mark.parametrize("ntee_code,name", INCLUDE_CASES)
def test_include_v2_ntee_codes(ntee_code, name):
    assert filter_sector(ntee_code, name) is True


def test_ntee_prefix_match_allows_subcodes():
    assert filter_sector("P4601", "Example Shelter With Subcode") is True


def test_ntee_prefix_does_not_match_substring_not_at_start():
    assert filter_sector("XP46", "Example Org") is False


# ── Codes dropped from v1 -> v2 must now be excluded absent a name match ──
@pytest.mark.parametrize(
    "ntee_code,name",
    [
        ("E22", "Example Health System"),
        ("U30", "Example Research Institute"),
        ("B40", "Example Professional Development Fund"),
        ("S31", "Example Economic Development Program"),
        ("L20", "Example Housing Trust"),
        ("W30", "Example Microfinance Fund"),
        ("I21", "Example Youth Center"),
        ("P44", "Example Supportive Housing Program"),
    ],
)
def test_v1_dropped_ntee_codes_excluded_without_name_match(ntee_code, name):
    assert filter_sector(ntee_code, name) is False


@pytest.mark.parametrize(
    "ntee_code,name",
    [
        ("A20", "Example Arts Council"),
        ("Q30", "Example International Development Org"),  # no name-term either
        ("X99", "Example Unclassified Org"),
        ("", "Example Org With No NTEE Code"),
    ],
)
def test_exclude_unrelated_ntee_with_no_name_match(ntee_code, name):
    assert filter_sector(ntee_code, name) is False


# ── NTEE hard-exclude (unchanged from v1) ──
@pytest.mark.parametrize("ntee_code", ["R40", "R60"])
def test_exclude_voting_ntee_codes(ntee_code):
    assert filter_sector(ntee_code, "Example Org With A Neutral Name") is False


def test_exclude_ntee_wins_even_with_name_match():
    """Hard reject beats BOTH inclusion paths: R60 NTEE + a name that would
    otherwise qualify via the women-term supplement."""
    assert filter_sector("R60", "Women's Community Action Alliance") is False


# ── Voting name regex (unchanged from v1) ──
def test_exclude_league_of_women_voters_still_excluded():
    """Regression (explicitly requested): unchanged from v1 -- hard reject
    even though NTEE R24 is included AND the name matches the women-term
    supplement regex ('women')."""
    assert (
        filter_sector("R24", "League of Women Voters of California Education Fund")
        is False
    )


@pytest.mark.parametrize(
    "name",
    [
        "Ohio Vote Project",
        "Get Out The Vote Coalition",
        "Women Voter Education Fund",
        "Election Integrity Project",
        "Ballot Access Coalition",
        "Electoral Reform Institute",
        "League of Women Voters of Texas",
        "VOTE Women Ohio",
    ],
)
def test_exclude_by_voting_regex_even_with_included_ntee(name):
    assert filter_sector("R24", name) is False


# ── New in v2: name-based supplement ──
@pytest.mark.parametrize(
    "name",
    [
        "Anita Borg Institute for Women and Technology",   # women
        "Global Fund for Women",                            # women
        "Girls Who Code Inc",                                # girls
        "Girl Scouts of the USA",                            # girl scouts (phrase)
        "Downtown Women's Center",                           # women's
        "Womens Initiative for Self Employment",             # womens
        "Association for Women in Science",                  # woman... women
        "Ovarian Cancer Research Alliance",                  # ovarian
        "National Breast Cancer Coalition Fund",             # breast cancer (phrase)
        "Society for Maternal-Fetal Medicine Foundation",    # maternal
        "Every Mother's Maternity Support Network",          # maternity
        "Alpha Kappa Alpha Sorority Inc",                    # sorority
        "Soroptimist International of the Americas",         # soroptimist
        "Zonta International",                               # zonta
        "YWCA USA Inc",                                       # ywca
        "Junior League of Chicago",                          # junior league (phrase)
        "The Female Health Company Foundation",              # female
    ],
)
def test_name_supplement_regex_includes_generically_coded_orgs(name):
    """These all carry an NTEE code that is NOT in the v2 include list (or
    none at all) -- inclusion here can only come from the name supplement."""
    assert filter_sector("A99", name) is True


def test_dignity_health_e22_excluded():
    """Regression (explicitly requested): E22 is no longer an include code
    (v2 rationale: E22 mostly tags general hospital systems, not
    women-specific care), and "Dignity Health" matches no women-term."""
    assert filter_sector("E22", "Dignity Health") is False


def test_soroptimist_i70_included():
    """Regression (explicitly requested): I70 is a v2 include code, and the
    name also independently matches the supplement regex -- both paths
    apply, see test_match_reason_both below."""
    assert filter_sector("I70", "Soroptimist International") is True


def test_breast_cancer_research_included_via_name_supplement():
    """Regression (explicitly requested): no qualifying NTEE code, included
    purely because the name matches the 'breast cancer' phrase."""
    assert filter_sector("", "Breast Cancer Research Foundation") is True


# ── Anti-pattern guard ──
def test_anti_pattern_women_and_men_style_name_is_fine():
    """The women-term need only be present -- co-mention of men doesn't
    disqualify a name match."""
    assert filter_sector("A99", "Coalition for Women and Men in Leadership") is True


def test_anti_pattern_no_women_term_is_not_included_via_name():
    """No women-term hit at all -- name path contributes nothing (org may
    still be included via NTEE, but not via this mechanism)."""
    assert filter_sector("A99", "Men's Health Network") is False


def test_name_supplement_no_false_positive_on_substrings():
    """Word-boundary sanity: 'women' must appear as a whole word, not as a
    substring of an unrelated word."""
    assert WOMENS_NAME_SUPPLEMENT_RE.search("Empowerment Zone Council") is None
    assert filter_sector("A99", "Empowerment Zone Council") is False


# ── match_reason ──
def test_match_reason_ntee_only():
    assert match_reason("P46", "Example Domestic Violence Shelter") == "ntee"


def test_match_reason_name_only():
    assert match_reason("", "Breast Cancer Research Foundation") == "name"


def test_match_reason_both():
    assert match_reason("I70", "Soroptimist International") == "both"


def test_match_reason_none_when_excluded():
    assert match_reason("A99", "Example Unrelated Org") is None
    assert match_reason("R60", "Women's Community Action Alliance") is None
