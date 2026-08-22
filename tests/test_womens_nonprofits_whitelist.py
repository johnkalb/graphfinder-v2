"""TDD (red phase) for specifications/womens-nonprofits-directory.md,
Validation section -- v2 (2026-08-21 amendment: "TDD v2" says "whitelist:
name-match instead of EIN-match (seed EINs hallucinated)").

The Gemini seed data's EINs are not trustworthy (the whole artifact was a
"hallucinated delivery" per the spec's Origin line) -- so identity/keying and
failure messages here are by ORG NAME, not EIN. EIN is still present in the
fixture and shown parenthetically in messages for cross-reference, but it is
never the thing an assertion is keyed or sorted on.

Fixture: tests/fixtures/womens_issues_nonprofits_seed.json, a verbatim copy
of the 31-record seed file the spec references (originally at
C:\\Users\\johnk\\Downloads\\greencollab_extracted\\scripts\\womens_issues_nonprofits.json).

Expected contract: `womens_nonprofits_pipeline.filter_sector(ntee_code, name)`
(see test_womens_nonprofits_filter.py for the full v2 filter contract,
including the NTEE include-list narrowing and the new name-supplement regex).

v1->v2 whitelist status change (simulated against the v2 rules before
writing this):
  - RESOLVED: "Women for Women International" (NTEE Q30) was the sole v1 gap
    (Q30 never in the include list). v2's name-supplement regex now matches
    "Women" in the name, so this record passes normally -- no xfail needed
    for it anymore.
  - NEW GAPS (4): v2's NTEE include list dropped E22/U30/B40/S31/L20/W30/I21/P44
    entirely (empirically shown fabricated), and these four seed orgs have
    neither a surviving NTEE include code NOR any word in
    WOMENS_NAME_SUPPLEMENT_RE anywhere in their name:
      - "Polaris Project Inc"        (NTEE I21 -- anti-trafficking, name has no women-term)
      - "N Street Village Inc"       (NTEE L20 -- name has no women-term)
      - "Dress for Success Worldwide"(NTEE S31 -- name has no women-term)
      - "Sarah's Circle Inc"         (NTEE L20 -- "Sarah's" isn't a matched term)
    All four are genuinely women-serving per their mission text (not
    mistagged the way Q30 was) -- they're just named/coded in a way v2's
    deterministic rules can't catch. Flagged via xfail below, not
    special-cased into the filter itself -- whether to add name-specific
    carve-outs or accept the recall loss is a spec decision.
"""
import json
from pathlib import Path

import pytest

from womens_nonprofits_pipeline import filter_sector, VOTING_NAME_RE, NTEE_INCLUDE_PREFIXES

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "womens_issues_nonprofits_seed.json"

# Known v2 recall gaps, keyed by NAME (not EIN -- seed EINs are hallucinated).
KNOWN_V2_GAPS = {
    "Polaris Project Inc": "NTEE I21 dropped in v2, name has no women-term",
    "N Street Village Inc": "NTEE L20 dropped in v2, name has no women-term",
    "Dress for Success Worldwide": "NTEE S31 dropped in v2, name has no women-term",
    "Sarah's Circle Inc": "NTEE L20 dropped in v2, \"Sarah's\" isn't a matched term",
}


def _load_seed_records():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["records"]


def test_fixture_has_31_records():
    records = _load_seed_records()
    assert len(records) == 31


@pytest.mark.parametrize(
    "record",
    _load_seed_records(),
    ids=lambda r: r["name"],
)
def test_seed_org_passes_filter(record):
    """Recall check: every seed org must be INCLUDED by filter_sector,
    except the four documented v2 gaps in KNOWN_V2_GAPS (see module
    docstring)."""
    if record["name"] in KNOWN_V2_GAPS:
        pytest.xfail(KNOWN_V2_GAPS[record["name"]])
    assert filter_sector(record["ntee_code"], record["name"]) is True, (
        f'"{record["name"]}" (NTEE {record["ntee_code"]}, seed EIN {record["ein"]} -- '
        "EIN not authoritative) was excluded by filter_sector -- check "
        "NTEE_INCLUDE_PREFIXES, WOMENS_NAME_SUPPLEMENT_RE, and VOTING_NAME_RE "
        "against this record"
    )


def test_no_seed_org_name_trips_the_voting_regex():
    """Sanity check: none of the 31 seed orgs should look like a
    voting/elections org by name alone."""
    records = _load_seed_records()
    offenders = [r["name"] for r in records if VOTING_NAME_RE.search(r["name"])]
    assert offenders == []


def test_all_seed_orgs_covered_by_filter_sector_except_known_v2_gaps():
    """Single-assertion summary of overall recall (NTEE-or-name, matching
    filter_sector's actual logic) -- surfaces any *new* coverage regression
    beyond the four documented v2 gaps without needing to read individual
    parametrized failures."""
    records = _load_seed_records()
    uncovered = sorted(
        r["name"] for r in records if not filter_sector(r["ntee_code"], r["name"])
    )
    assert uncovered == sorted(KNOWN_V2_GAPS), (
        f"Seed orgs failing filter_sector: {uncovered} -- expected exactly "
        f"the known v2 gaps {sorted(KNOWN_V2_GAPS)}. If this list grew, "
        "something regressed; if it shrank, update KNOWN_V2_GAPS."
    )


def test_ntee_only_coverage_is_narrower_than_full_filter_v2():
    """Documents the v2 rationale directly: several seed orgs pass ONLY
    because of the name supplement, not because of their NTEE code -- i.e.
    NTEE-only coverage is strictly narrower than filter_sector's actual
    (ntee-or-name) recall. If this ever becomes empty, the name supplement
    isn't pulling its weight and something's probably wrong."""
    records = _load_seed_records()
    ntee_only_uncovered = {
        r["name"]
        for r in records
        if not any(r["ntee_code"].startswith(p) for p in NTEE_INCLUDE_PREFIXES)
    }
    name_rescued = ntee_only_uncovered - set(KNOWN_V2_GAPS)
    assert len(name_rescued) > 0, (
        "Expected at least one seed org to be rescued by the name supplement "
        "alone (e.g. 'Women for Women International', NTEE Q30) -- got none."
    )
