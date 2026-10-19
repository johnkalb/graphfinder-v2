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
    ],
    "cmte_totals": [("C001", "OBAMA FOR AMERICA", 3, 75000.0), ("C002", "SOME PAC", 1, 5000.0)],
    "donors": [
        ("C001", "OBAMA FOR AMERICA", "Jane Q Donor", "P", 50000.0, 4, "2008,2012", "ACME", "CEO", "NY"),
        ("C001", "OBAMA FOR AMERICA", "Warren Buffett", "P", 20000.0, 2, "2008", "BERKSHIRE", "INVESTOR", "NE"),
        ("C002", "SOME PAC", "Warren Buffett", "P", 5000.0, 1, "2012", "BERKSHIRE", "INVESTOR", "NE"),
        ("C001", "OBAMA FOR AMERICA", "Small Giver", "P", 5000.0, 1, "2012", "", "", "CA"),
    ],
    "pac_flows": [("SOME PAC", "OBAMA FOR AMERICA", "contribution", 5000.0, 1, "2008")],
    "vendors": [("OBAMA FOR AMERICA", "GMMB INC", 1250000.5, 30, "2008,2012", "MEDIA BUY")],
    "leadership": [
        ("Goldman Sachs Group Inc", "David Solomon", "CEO", "SEC_FORM_4", "2024"),
        ("Goldman Sachs Group Inc", "David Solomon", "Director", "LITTLESIS", ""),
        ("Goldman Sachs Group Inc", "Private Officer", "Officer", "SEC_FORM_D", ""),
        ("Tiny Charity", "Private Officer", "Board member", "IRS_990", ""),
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
    assert set(solomon["titles"].split(",")) == {"CEO", "Director"} and solomon["year"] == "2024"
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
