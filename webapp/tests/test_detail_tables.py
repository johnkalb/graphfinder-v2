"""Detail tables (private-service spec Phase 2): deterministic TSV export, the
SQLite loader, the lookups, and /api/my/detail's beta gate and decision-7 rule."""
import os

import pytest

import detail_tables as dt

pytestmark = pytest.mark.timeout(30)

ROWS = {
    "committees": [
        ("C001", "OBAMA FOR AMERICA", "Barack Obama", "candidate", "P80003338"),
        ("C002", "SOME PAC", "ACME CORP", "sponsor", ""),
        ("C007", "KEN PAXTON FOR SENATE", "Warren Kenneth Paxton Jr", "candidate", "S6TX00000"),
        ("C008", "SMITH FOR CONGRESS", "John Arthur Smith", "candidate", "H0XX00001"),
        ("C009", "SMITH 2026", "John Lee Smith", "candidate", "H0XX00002"),
    ],
    "cmte_totals": [("C001", "OBAMA FOR AMERICA", 3, 75000.0), ("C002", "SOME PAC", 1, 5000.0),
                    ("C007", "KEN PAXTON FOR SENATE", 1, 25000.0)],
    "donors": [
        ("C001", "OBAMA FOR AMERICA", "Jane Q Donor", "P", 50000.0, 4, "2008,2012", "ACME", "CEO", "NY"),
        ("C001", "OBAMA FOR AMERICA", "Warren Buffett", "P", 20000.0, 2, "2008", "BERKSHIRE", "INVESTOR", "NE"),
        ("C002", "SOME PAC", "Warren Buffett", "P", 5000.0, 1, "2012", "BERKSHIRE", "INVESTOR", "NE"),
        ("C001", "OBAMA FOR AMERICA", "Small Giver", "P", 5000.0, 1, "2012", "", "", "CA"),
        ("C003", "SLF PAC", "Kenneth C Griffin", "P", 100000000.0, 3, "2022", "CITADEL", "CEO", "FL"),
        ("C004", "CLF", "Kenneth Griffin", "P", 60000000.0, 2, "2022", "CITADEL", "CEO", "FL"),
        ("C005", "NRCC", "Ken Griffin", "P", 10000.0, 1, "2018", "CITADEL", "CEO", "IL"),
        ("C005", "NRCC", "Kenneth Griffiths", "P", 10000.0, 1, "2018", "", "", "OH"),
        ("C007", "KEN PAXTON FOR SENATE", "Big Texan", "P", 25000.0, 1, "2026", "OIL CO", "CEO", "TX"),
        ("C006", "REPUBLICAN PARTY OF TEXAS", "Ken Paxton", "P", 1000.0, 1, "2012", "SELF", "LAWYER", "TX"),
    ],
    "pac_flows": [("SOME PAC", "OBAMA FOR AMERICA", "contribution", 5000.0, 1, "2008")],
    "vendors": [("OBAMA FOR AMERICA", "GMMB INC", 1250000.5, 30, "2008,2012", "MEDIA BUY")],
    "leadership": [
        ("Goldman Sachs Group Inc", "David Solomon", "CEO", "SEC_FORM_4", "2024", "", "", ""),
        ("Goldman Sachs Group Inc", "David Solomon", "Director", "LITTLESIS", "", "", "", ""),
        ("Goldman Sachs Group Inc", "Private Officer", "Officer", "SEC_FORM_D", "", "", "", ""),
        ("Tiny Charity", "Private Officer", "Board member", "IRS_990", "", "", "", ""),
        ("Tiny Charity", "KEN PAXTON", "Board member", "IRS_990", "2022", "", "", ""),
        # GM: LittleSis (dated and undated) and SEC Form 3/4/5 rows under three spellings of the company
        ("General Motors Company", "mary barra", "Board member", "LITTLESIS", "", "2010", "2014", "no"),
        ("General Motors Co", "Mary T Barra", "Director, Chair & CEO", "SEC_FORM_345", "2026", "2014", "2026", "yes"),
        ("General Motors Company", "Theodore M Solso", "Chairman", "LITTLESIS", "", "", "", "yes"),
        ("GENERAL MOTORS CORP", "John Smale", "Director", "SEC_FORM_345", "2008", "2006", "2008", "no"),
        ("General Motors Company", "Old Exec", "Position", "LITTLESIS", "", "", "", ""),
    ],
    "lobbying": [("Kristian Chin", "ACME CORP", "SMITH GARSON", "2026", "Budget/Appropriations",
                  "Staff Assistant, Rep. Ruiz")],
}


@pytest.fixture
def store(tmp_path):
    src = tmp_path / "detail"
    src.mkdir()
    for t, rows in ROWS.items():
        dt.write_tsv(str(src / dt.tsv_name(t)), t, rows)
    s = dt.DetailStore(src, tmp_path / "detail.sqlite")
    s.load(log=lambda m: None)
    return s


def test_tsv_is_deterministic_and_clean(tmp_path):
    a, b = tmp_path / "a.tsv.gz", tmp_path / "b.tsv.gz"
    rows = [("C1", "X\tY", "Line\nbreak", "P", 1.5, 1, "2008", None, "", "NY")]
    dt.write_tsv(str(a), "donors", rows)
    dt.write_tsv(str(b), "donors", rows)
    assert a.read_bytes() == b.read_bytes()
    import gzip
    lines = gzip.open(a, "rt", encoding="utf-8").read().splitlines()
    assert lines[1].split("\t")[1:3] == ["X Y", "Line break"] and len(lines) == 2
    with pytest.raises(ValueError):
        dt.write_tsv(str(a), "donors", [("too", "short")])


def test_loads_every_table(store):
    assert store.status()["ready"] == {t: len(r) for t, r in ROWS.items()}
    assert not store.status()["errors"]


def test_top_donors_by_candidate_or_committee(store):
    for name in ("Barack Obama", "Obama for America"):
        r = store.top_donors([dt.canon_key(name)])
        assert [d["donor"] for d in r["donors"]] == ["Jane Q Donor", "Warren Buffett", "Small Giver"]
        assert r["total_donors"] == 3 and r["total_usd"] == 75000.0
    assert store.top_donors(["nobody"])["donors"] == []


def test_donations_by_a_donor_with_candidate(store):
    r = store.donations_by([dt.canon_key("WARREN BUFFETT")])
    assert [(x["committee"], x["usd"], x["candidate"]) for x in r] == [
        ("OBAMA FOR AMERICA", 20000.0, "Barack Obama"), ("SOME PAC", 5000.0, "")]


def test_leadership_both_directions(store):
    people = store.leadership_of([dt.canon_key("Goldman Sachs Group, Inc.")])
    solomon = next(p for p in people if p["person"] == "David Solomon")
    assert set(solomon["titles"].split(",")) == {"CEO", "Director"} and solomon["years"] == "2024"
    assert {r["org"] for r in store.roles_of([dt.canon_key("private officer")])} == {"Goldman Sachs Group Inc", "Tiny Charity"}


def test_money_vendor_lobbying_and_suggest(store):
    assert store.pac_flows([dt.canon_key("Some PAC")])["gave"][0]["other"] == "OBAMA FOR AMERICA"
    assert store.vendors([dt.canon_key("gmmb inc")])["paid_by"][0]["usd"] == 1250000.5
    lob = store.lobbying([dt.canon_key("Acme Corp")])
    assert lob["as_client"][0]["registrant"] == "SMITH GARSON" and lob["as_lobbyist"] == []
    assert "Goldman Sachs Group Inc" in store.suggest("goldman")


def test_reload_only_changed_tables(store, tmp_path):
    logs = []
    store.load(log=logs.append)
    assert logs == []                      # nothing changed: nothing reloaded
    dt.write_tsv(os.path.join(store.src_dir, dt.tsv_name("vendors")), "vendors",
                 ROWS["vendors"] + [("SOME PAC", "PRINT SHOP", 10000.0, 1, "2010", "PRINTING")])
    store.load(log=logs.append)
    assert len(logs) == 1 and "vendors: 2 rows" in logs[0]
    assert store.status()["ready"]["vendors"] == 2


def test_missing_table_is_not_loaded(tmp_path):
    s = dt.DetailStore(tmp_path / "none", tmp_path / "x.sqlite")
    s.load(log=lambda m: None)
    assert s.status()["ready"] == {} and s.donations_by(["x"]) is None


# --- HTTP layer -----------------------------------------------------------------
from test_tester_api import client, pf, tester_data_dir  # noqa: E402,F401 -- fixtures are used by name

USER = {"Cf-Access-Authenticated-User-Email": "detail.user@example.com"}


@pytest.fixture
def api(client, store, monkeypatch):
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "all")
    monkeypatch.setattr(pf, "_detail_store", store)
    monkeypatch.setattr(pf, "_resolve_name", lambda x: None)
    monkeypatch.setattr(pf, "_load_demo_verified", lambda: {"warren buffett", "david solomon"})
    client.post("/api/agreement/accept", headers=USER)
    return client


def test_gate(client, api, monkeypatch):
    assert client.get("/api/my/detail?kind=donors&name=Obama").status_code == 401
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "admins")
    assert client.get("/api/my/detail?kind=donors&name=Obama", headers=USER).status_code == 404


def test_api_lookups(api):
    d = api.get("/api/my/detail?kind=donors&name=Barack Obama", headers=USER).json()
    assert d["result"]["donors"][0]["donor"] == "Jane Q Donor"
    d = api.get("/api/my/detail?kind=donations&name=Warren Buffett", headers=USER).json()
    assert len(d["result"]) == 2
    assert api.get("/api/my/detail?kind=bogus&name=x y", headers=USER).status_code == 400
    d = api.get("/api/my/detail?kind=leadership&name=Goldman", headers=USER).json()
    assert "Goldman Sachs Group Inc" in d["suggestions"]


def test_private_people_get_no_profile(api):
    """Decision 7: no donations or roles for an unverified person."""
    d = api.get("/api/my/detail?kind=donations&name=Jane Q Donor", headers=USER).json()
    assert "refused" in d and d["result"] == []
    d = api.get("/api/my/detail?kind=leadership&name=Private Officer", headers=USER).json()
    assert "refused" in d and d["result"]["roles"] == []
    d = api.get("/api/my/detail?kind=leadership&name=David Solomon", headers=USER).json()
    assert {r["org"] for r in d["result"]["roles"]} == {"Goldman Sachs Group Inc"}
    # an organization's board lists its people by name (menu question 6)
    d = api.get("/api/my/detail?kind=leadership&name=Goldman Sachs Group Inc", headers=USER).json()
    assert {p["person"] for p in d["result"]["people"]} == {"David Solomon", "Private Officer"}


def test_person_variants_find_nickname_and_middle_initial_spellings(store):
    keys = [dt.canon_key("Ken Griffin")]
    v = store.person_variants(keys)
    assert set(v) == {"kenneth griffin", "kenneth c griffin"}            # not "kenneth griffiths"
    rows = store.donations_by(keys + v)
    assert [r["committee"] for r in rows] == ["SLF PAC", "CLF", "NRCC"]
    assert rows[0]["recorded_as"] == "Kenneth C Griffin"
    assert store.person_variants([dt.canon_key("Goldman Sachs Group Inc")]) == []


def test_board_years_status_and_spellings_merged(store):
    """2026-10-20: 'who is on the GM board' -- current people first, with years;
    'mary barra' (LittleSis) and 'Mary T Barra' (SEC) are one row; General
    Motors / Co / Company / Corp are one organization."""
    keys = [dt.canon_key("General Motors")]
    people = store.leadership_of(keys + store.org_variants(keys))
    assert [(p["person"], p["status"], p["years"]) for p in people] == [
        ("Mary T Barra", "current", "2010–2026"),
        ("Theodore M Solso", "listed as current", ""),
        ("Old Exec", "", ""),
        ("John Smale", "former (last SEC filing 2008)", "2006–2008"),
    ]
    assert people[0]["recorded_as"] == "Mary T Barra,mary barra"
    roles = store.roles_of([dt.canon_key("Mary Barra"), dt.canon_key("Mary T Barra")])
    assert len(roles) == 1 and roles[0]["status"] == "current"


def test_old_leadership_file_loads_with_blank_new_columns(tmp_path):
    import gzip
    src = tmp_path / "detail"
    src.mkdir()
    with gzip.open(src / dt.tsv_name("leadership"), "wt", encoding="utf-8") as f:
        f.write("org\tperson\ttitle\tsource\tyear\nAcme\tJo Bloggs\tDirector\tIRS_990\t2020\n")
    s = dt.DetailStore(src, tmp_path / "d.sqlite")
    s.load(log=lambda m: None)
    assert not s.status()["errors"]
    assert s.leadership_of(["acme"]) == [{"person": "Jo Bloggs", "titles": "Director", "years": "2020",
                                          "status": "", "sources": "IRS_990"}]


def test_suggest_is_one_per_name_whatever_the_case(store):
    sug = store.suggest("ken paxton")
    assert sug == ["KEN PAXTON FOR SENATE", "Ken Paxton"]


def test_candidate_filed_under_a_fuller_name(store):
    keys = ["ken paxton"] + store.person_variants(["ken paxton"])
    assert store.candidate_matches(keys) == ["Warren Kenneth Paxton Jr"]
    assert store.candidate_matches(["john smith"]) == ["John Arthur Smith", "John Lee Smith"]
    assert store.candidate_matches(["paxton warren"]) == []   # the last name must match


def test_api_donors_to_a_candidate_by_common_name(api):
    d = api.get("/api/my/detail?kind=donors&name=Ken Paxton", headers=USER).json()
    assert d["matched_as"] == "Warren Kenneth Paxton Jr"
    assert [x["donor"] for x in d["result"]["donors"]] == ["Big Texan"]
    d = api.get("/api/my/detail?kind=donors&name=John Smith", headers=USER).json()
    assert "matched_as" not in d and d["suggestions"][:2] == ["John Arthur Smith", "John Lee Smith"]
