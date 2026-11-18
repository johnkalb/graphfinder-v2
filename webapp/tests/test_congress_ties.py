"""Congress tie-strength pilot (2026-11-18): date helpers, explanations, the
scoring categories and the evidence endpoint. The fitted model itself is
checked by congress_ties.py's own report, not here."""
import os
import sys
from datetime import date

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import congress_ties as ct  # noqa: E402
from link_scoring import CATEGORY_DESC, score_pair  # noqa: E402
from relation_categories import categorize  # noqa: E402


def test_congress_numbers_and_years():
    assert ct.congress_of(date(1993, 1, 5)) == 103
    assert ct.congress_of(date(1995, 1, 2)) == 103          # before 3 January: still the old Congress
    assert ct.congress_of(date(2026, 11, 18)) == 119
    assert ct.congress_years(116) == (2019, 2021)
    assert ct.years_label([116, 117]) == "2019–2023"
    assert ct.years_label([104, 105, 110]) == "1995–1999, 2007–2009"


def test_explanation_reads_like_a_reason():
    meta = {"c1": {"kind": "committee", "congress": 116, "name": "House Financial Services"},
            "c2": {"kind": "committee", "congress": 117, "name": "House Financial Services"},
            "s1": {"kind": "subcommittee", "congress": 117, "name": "House Financial Services: Capital Markets"},
            "d1": {"kind": "delegation", "congress": 116, "name": "CA congressional delegation"}}
    sv = {"A": {116: ("H", "Democrat", "CA"), 117: ("H", "Democrat", "CA")},
          "B": {116: ("H", "Democrat", "CA"), 117: ("H", "Democrat", "CA")}}
    text = ct.explain("A", "B", {"ctx": ["c1", "c2", "s1", "d1"]}, sv, {("A", "B"): 14}, {}, None, meta, "strong")
    assert text == ("House Financial Services: Capital Markets (2021–2023); House Financial Services (2019–2023); "
                    "CA congressional delegation (2019–2021); both Democrats; co-sponsored 14 bills together")
    weak = ct.explain("A", "B", {"ctx": []}, sv, {}, {}, None, meta, "weak")
    assert weak == "Weak tie: both Democrats"


def test_name_variants_cover_nicknames_and_middles():
    m = {"name": "Charles E. Schumer", "first": "Charles", "middle": "Ellis", "last": "Schumer", "nick": "Chuck",
         "suffix": ""}
    assert {"chuck schumer", "charles schumer", "charles e schumer"} <= ct.name_variants(m)


def test_categories_and_scoring():
    assert categorize("CONGRESS_COLLEAGUE") == "CONGRESS_COLLEAGUE"
    assert categorize("CONGRESS_WEAK_TIE") == "CONGRESS_WEAK_TIE"
    assert "weak" in CATEGORY_DESC["CONGRESS_WEAK_TIE"][0].lower()
    p, used = score_pair(["CONGRESS_COLLEAGUE"])
    assert 0 < p < 1 and used == ["CONGRESS_COLLEAGUE"]


def test_evidence_endpoint_explains_congress_links(client, monkeypatch):
    monkeypatch.setattr(pf, "_congress_ties", {"barney frank|spencer bachus": [
        "House Financial Services (1995–2013); co-sponsored 9 bills together", 0.71]})
    r = client.get("/api/evidence", params={"src": "Spencer Bachus", "tgt": "Barney Frank",
                                            "rel": "CONGRESS_COLLEAGUE"}).json()
    ev = r["evidence"][0]
    assert "House Financial Services (1995–2013)" in ev["snippet"] and "71%" in ev["snippet"]
    # other relations are unaffected
    r = client.get("/api/evidence", params={"src": "Spencer Bachus", "tgt": "Barney Frank", "rel": "DONATION"}).json()
    assert "Congressional records" not in str(r)


def test_chair_and_ranking_member_named_first():
    meta = {"k1": {"kind": "committee", "congress": 110, "name": "Senate Banking, Housing, and Urban Affairs"},
            "k2": {"kind": "committee", "congress": 111, "name": "Senate Banking, Housing, and Urban Affairs"}}
    members = {"k1": {"D": "chair", "S": "ranking"}, "k2": {"D": "chair", "S": "ranking"}}
    sv = {"D": {110: ("S", "Democrat", "CT"), 111: ("S", "Democrat", "CT")},
          "S": {110: ("S", "Republican", "AL"), 111: ("S", "Republican", "AL")}}
    text = ct.explain("D", "S", {"ctx": ["k1", "k2"], "chair_rank": 2}, sv, {}, {}, None, meta, "strong", members)
    assert text == "chair and ranking member of Senate Banking, Housing, and Urban Affairs (2007–2011)"
