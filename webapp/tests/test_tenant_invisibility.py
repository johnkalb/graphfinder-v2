"""Another user must see no trace of someone's private or team links -- not
even through the shared narrative cache (2026-11-15)."""
from test_tester_api import client, pf, tester_data_dir  # noqa: F401


def _path(rel):
    return {"path": [{"node": "Ann Bee", "label": "Ann Bee", "relation": rel},
                     {"node": "Jane Doe", "label": "Jane Doe", "relation": "EMPLOYMENT"},
                     {"node": "Acme Corp", "label": "Acme Corp"}], "length": 2, "guided_probability": 0.1}


def test_private_paths_never_enter_the_shared_narrative_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(pf, "_generate_path_narrative", lambda p: calls.append(1) or "story")
    monkeypatch.setattr(pf, "_narrative_cache", {})
    monkeypatch.setattr(pf, "_narrative_cache_order", [])
    for rel in ("MY_LINK", "TEAM_CONTACT"):
        p = _path(rel)
        assert pf._generate_and_cache_narrative(p) == "story"
        assert pf._get_cached_narrative(p) is None
    assert pf._narrative_cache == {} and len(calls) == 2
    # a member-labelled team step is private even if its relation was rewritten
    p = _path("EMPLOYMENT")
    p["path"][0]["team_members"] = ["Bob Builder"]
    assert pf._path_has_private_step(p["path"])
    # ordinary public paths are still cached
    pub = _path("CO_DIRECTOR")
    pf._generate_and_cache_narrative(pub)
    pf._generate_and_cache_narrative(pub)
    assert len(calls) == 4 - 1 and pf._get_cached_narrative(pub) == "story"


def test_one_time_bulk_allowance_for_contact_matching(client, monkeypatch):
    from contact_psi import app as psi
    h = {"Cf-Access-Authenticated-User-Email": "bulk@team.example"}
    monkeypatch.setattr(psi, "DAILY_MAX_POINTS", 10)
    monkeypatch.setattr(pf, "PSI_BULK_MAX_POINTS", 25)
    psi._usage_counters.clear()
    q = client.get("/api/contacts/quota", headers=h).json()
    assert q["daily_points"] == 10 and q["bulk_points"] == 25
    assert psi.consume_quota("bulk@team.example", 10)          # the day's quota is used up
    assert pf._psi_bulk_take("bulk@team.example", 20)          # the bulk allowance carries on
    assert not pf._psi_bulk_take("bulk@team.example", 10)      # ...up to its size
    assert client.get("/api/contacts/quota", headers=h).json()["bulk_points"] == 5
    assert not pf._psi_bulk_take("anonymous", 1)               # never for anonymous callers
    # it closes 24 hours after it started, and a site admin can grant another
    import db as _db
    conn = _db.connect(pf._ml_db())
    conn.execute("UPDATE psi_bulk SET started_at = '2020-01-01T00:00:00+00:00' WHERE email = ?", ("bulk@team.example",))
    conn.commit()
    conn.close()
    assert pf._psi_bulk_left("bulk@team.example") == 0
    admin = {"Cf-Access-Authenticated-User-Email": "kureious09@gmail.com"}
    assert client.post("/api/admin/psi-bulk-reset", json={"email": "bulk@team.example"}, headers=h).status_code in (401, 403)
    assert client.post("/api/admin/psi-bulk-reset", json={"email": "bulk@team.example"}, headers=admin).status_code == 200
    assert pf._psi_bulk_left("bulk@team.example") == 25
    psi._usage_counters.clear()
