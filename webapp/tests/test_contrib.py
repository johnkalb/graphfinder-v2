"""'Where have you been?' affiliations and where they put a user in the graph
(webapp/contrib.py, plan peppy-imagining-lynx.md Phase 1)."""
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parent.parent
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import contrib as cb  # noqa: E402
from test_department import init_test_department_db  # noqa: E402

# A graph with one big organization split into 4-year periods and one small one.
NODES = {"Acme Corp", "Acme Corp (in 2012–2015)", "Acme Corp (in 2016–2019)", "Acme Corp (in 2020–2023)",
         "Tiny Nonprofit"}
has = NODES.__contains__


@pytest.fixture
def dbp(tmp_path):
    p = str(tmp_path / "td.db")
    init_test_department_db(p)
    return p


def test_affiliations_are_the_owners_only_and_deletable(dbp):
    row, err = cb.add_affiliation(dbp, "a@x.org", "Acme Corp", 2017, 2019, now_year=2026)
    assert err is None and row["start_year"] == 2017
    assert [a["org"] for a in cb.list_affiliations(dbp, "a@x.org")] == ["Acme Corp"]
    assert cb.list_affiliations(dbp, "b@x.org") == []
    assert not cb.delete_affiliation(dbp, "b@x.org", row["id"])      # not yours
    assert cb.delete_affiliation(dbp, "a@x.org", row["id"])
    assert cb.list_affiliations(dbp, "a@x.org") == []


def test_years_are_checked_and_swapped(dbp):
    assert cb.add_affiliation(dbp, "a@x.org", "Acme Corp", 1800, None, now_year=2026)[1]
    assert cb.add_affiliation(dbp, "a@x.org", "Acme Corp", "soon", None, now_year=2026)[1]
    row, err = cb.add_affiliation(dbp, "a@x.org", "Acme Corp", 2019, 2016, now_year=2026)
    assert err is None and (row["start_year"], row["end_year"]) == (2016, 2019)


def test_cap(dbp):
    for i in range(cb.MAX_AFFILIATIONS):
        assert cb.add_affiliation(dbp, "a@x.org", f"Org {i}")[1] is None
    assert cb.add_affiliation(dbp, "a@x.org", "One too many")[1]


def test_matching_years_join_the_period_strongly_and_the_org_weakly():
    ents = cb.entry_points([{"id": 1, "org": "Acme Corp", "start_year": 2017, "end_year": 2018}], has, now_year=2026)
    got = {(e["node"], e["prob"]) for e in ents}
    assert ("Acme Corp (in 2016–2019)", cb.AFF_STRONG) in got
    assert ("Acme Corp", cb.AFF_WEAK) in got
    assert not any("2012" in e["node"] or "2020" in e["node"] for e in ents)


def test_long_tenure_joins_every_period_it_touches():
    ents = cb.entry_points([{"id": 1, "org": "Acme Corp", "start_year": 2014, "end_year": 2021}], has, now_year=2026)
    periods = {e["node"] for e in ents if e["kind"] == "period"}
    assert periods == {"Acme Corp (in 2012–2015)", "Acme Corp (in 2016–2019)", "Acme Corp (in 2020–2023)"}


def test_no_years_or_no_matching_period_is_only_weak():
    for aff in ({"id": 1, "org": "Acme Corp", "start_year": None, "end_year": None},
                {"id": 2, "org": "Acme Corp", "start_year": 1990, "end_year": 1995}):
        ents = cb.entry_points([aff], has, now_year=2026)
        assert [(e["node"], e["prob"]) for e in ents] == [("Acme Corp", cb.AFF_WEAK)]


def test_small_org_without_periods_is_full_strength():
    ents = cb.entry_points([{"id": 1, "org": "Tiny Nonprofit", "start_year": None, "end_year": None}], has)
    assert [(e["node"], e["prob"]) for e in ents] == [("Tiny Nonprofit", cb.AFF_STRONG)]


def test_unknown_org_is_skipped():
    assert cb.entry_points([{"id": 1, "org": "Nowhere Inc", "start_year": 2010, "end_year": 2012}], has) == []


def test_years_text():
    assert cb.years_text({"start_year": 2016, "end_year": 2019}) == "2016–2019"
    assert cb.years_text({"start_year": 2016, "end_year": None}) == "from 2016"
    assert cb.years_text({"start_year": None, "end_year": None}) == ""
