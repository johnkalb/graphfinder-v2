"""Contact lookup through the subscriber's own provider key (2026-11-15):
nothing returned is stored, protected people and people who blocked lookups
get no personal details, a certification comes first, and team keys follow
workspace membership."""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

import contact_lookup as cl

pytestmark = pytest.mark.timeout(30)

ADMIN = {"Cf-Access-Authenticated-User-Email": "kureious09@gmail.com"}
ANN = {"Cf-Access-Authenticated-User-Email": "ann@acme.example"}       # a subscriber
JUDY = {"Cf-Access-Authenticated-User-Email": "judy.ruiz@x.example"}    # Judy Ruiz, in the graph

NODES = {"jane doe": "Jane Doe", "judge dredd": "Judge Dredd", "mark greene (acme corp)": "Mark Greene (Acme Corp)",
         "judy ruiz": "Judy Ruiz", "acme corp": "Acme Corp"}
NEIGHBORS = {"Jane Doe": [("Acme Corp", ["EMPLOYMENT"]), ("Jane's PAC", ["DONATION"])],
             "Judge Dredd": [("Superior Court of Mega City", ["PUBLIC_OFFICE"])],
             "Judy Ruiz": [("Ruiz Bakery LLC", ["EMPLOYMENT"])],
             "Mark Greene (Acme Corp)": [("Springfield Police Department", ["DONATION"]),     # a donation isn't a role
                                         ("Federal Bureau of Investigation", ["PROFESSIONAL"]),  # nor an informant tie
                                         ("Springfield University", ["EDUCATION", "EMPLOYMENT"]),
                                         ("Acme Corp", ["CO_EXECUTIVE"])]}

PDL_HIT = {"status": 200, "likelihood": 8, "data": {
    "work_email": "jane@acme.com", "personal_emails": ["janed@gmail.com"], "mobile_phone": "+15551234567",
    "phone_numbers": ["+15550000000"], "location_street_address": "1 Elm St", "location_locality": "Seattle",
    "location_region": "washington", "location_postal_code": "98101"}}


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("CONTACT_LOOKUP_BETA", "all")
    monkeypatch.setenv("CONTACT_LOOKUP_SECRET", "test-secret")
    monkeypatch.setattr(pf, "_resolve_name", lambda q: NODES.get((q or "").strip().lower()))
    monkeypatch.setattr(pf, "_get_label", lambda n: n)
    monkeypatch.setattr(pf, "_lookup_neighbors", lambda n: NEIGHBORS.get(n, []))
    monkeypatch.setattr(pf, "_display_name_for_email",
                        lambda e: {"judy.ruiz@x.example": "Judy Ruiz", "ann@acme.example": "Ann Bee"}.get(e))
    calls = []

    def fake_http(method, url, params, headers):
        calls.append((method, url, dict(params), dict(headers)))
        return 200, PDL_HIT
    monkeypatch.setattr(cl, "http", fake_http)
    for h in (ADMIN, ANN, JUDY):
        assert client.post("/api/agreement/accept", headers=h).status_code == 200
    return calls


def _certify(client, h):
    r = client.post("/api/lookup/certify", json={"purpose": cl.PURPOSES[0], "accept": True}, headers=h)
    assert r.status_code == 200, r.text


def _key(client, h, provider="pdl", key="pdl-live-ABCD1234", ws_id=None):
    return client.put("/api/lookup/keys", json={"provider": provider, "key": key, "ws_id": ws_id}, headers=h)


def test_key_encryption_is_bound_to_owner_and_provider(monkeypatch):
    monkeypatch.setenv("CONTACT_LOOKUP_SECRET", "s")
    blob = cl.encrypt_key("sk-123456789", "a@b.co", None, "pdl")
    assert "sk-123456789" not in blob
    assert cl.decrypt_key(blob, "a@b.co", None, "pdl") == "sk-123456789"
    with pytest.raises(Exception):
        cl.decrypt_key(blob, "eve@b.co", None, "pdl")
    with pytest.raises(Exception):
        cl.decrypt_key(blob, "a@b.co", None, "hunter")


def test_parsers_and_work_mode_filter():
    out, items, conf, _ = cl._parse("pdl", 200, PDL_HIT)
    kinds = {(i["type"], i["kind"]) for i in items}
    assert out == "found" and conf == 8
    assert ("email", "work") in kinds and ("email", "personal") in kinds and ("address", "personal") in kinds
    assert cl._parse("pdl", 404, None)[0] == "no_match"
    assert cl._parse("hunter", 200, {"data": {"email": "j@acme.com", "score": 91,
                                              "verification": {"status": "valid"}}})[1][0]["value"] == "j@acme.com"
    assert cl._parse("hunter", 451, None)[0] == "opted_out"
    assert cl._parse("apollo", 200, {"person": {"match_confidence": "none"}})[0] == "no_match"
    ap = cl._parse("apollo", 200, {"person": {"match_confidence": "high", "email": "j@acme.com",
                                              "contact": {"contact_emails": [{"email": "j@gmail.com"}]}}})[1]
    assert [(i["value"], i["kind"]) for i in ap] == [("j@acme.com", "work"), ("j@gmail.com", "personal")]
    assert cl._parse("rocketreach", 200, {"status": "searching"})[0] == "pending"
    rr = cl._parse("rocketreach", 200, {"status": "complete", "emails": [{"email": "j@acme.com", "type": "professional"}],
                                        "phones": [{"number": "555", "type": "mobile"}]})[1]
    assert {(i["value"], i["kind"]) for i in rr} == {("j@acme.com", "work"), ("555", "personal")}
    with pytest.raises(cl.LookupError_):
        cl._parse("pdl", 401, None)


def test_flag_certification_and_never_stored(client, setup, monkeypatch):
    calls = setup
    monkeypatch.setenv("CONTACT_LOOKUP_BETA", "admins")
    assert client.get("/api/lookup/status", headers=ANN).status_code == 404
    monkeypatch.setenv("CONTACT_LOOKUP_BETA", "all")
    assert client.get("/api/lookup/status").status_code == 401
    assert _key(client, ANN).status_code == 200
    st = client.get("/api/lookup/status", headers=ANN).json()
    assert next(p for p in st["providers"] if p["id"] == "pdl")["my_key"] == "…1234"
    body = {"name": "Jane Doe", "provider": "pdl", "mode": "work", "company": "Acme Corp"}
    r = client.post("/api/lookup/run", json=body, headers=ANN)
    assert r.status_code == 428 and not calls
    _certify(client, ANN)
    r = client.post("/api/lookup/run", json=body, headers=ANN).json()
    assert r["outcome"] == "found" and [i["value"] for i in r["items"]] == ["jane@acme.com"]   # work mode: work only
    method, url, params, headers = calls[-1]
    assert headers["X-Api-Key"] == "pdl-live-ABCD1234" and params["name"] == "Jane Doe" and params["company"] == "Acme Corp"
    r = client.post("/api/lookup/run", json={**body, "mode": "personal"}, headers=ANN).json()
    assert {"janed@gmail.com", "+15551234567"} <= {i["value"] for i in r["items"]}
    # nothing that came back, nor the key, is anywhere in the database
    import db as _db
    conn = _db.connect(pf._ml_db())
    dump = " ".join(str(tuple(row)) for t in ("lookup_audit", "lookup_keys", "ws_audit")
                    for row in conn.execute(f"SELECT * FROM {t}").fetchall())
    conn.close()
    for secret in ("jane@acme.com", "janed@gmail.com", "5551234567", "1 Elm St", "pdl-live-ABCD1234"):
        assert secret not in dump
    hist = client.get("/api/lookup/history", headers=ANN).json()["lookups"]
    assert hist[0]["person"] == "Jane Doe" and hist[0]["mode"] == "personal" and hist[0]["n_items"] >= 3


def test_protected_people_get_no_personal_lookups(client, setup):
    calls = setup
    _certify(client, ANN)
    _key(client, ANN)
    p = client.get("/api/lookup/person", params={"name": "Judge Dredd"}, headers=ANN).json()
    assert p["protected"] and p["personal_blocked"] and p["links"]["personal"] == [] and p["links"]["work"]
    n = len(calls)
    r = client.post("/api/lookup/run", json={"name": "Judge Dredd", "provider": "pdl", "mode": "personal",
                                             "company": "Court"}, headers=ANN)
    assert r.status_code == 403 and r.json()["code"] == "protected" and len(calls) == n
    assert client.post("/api/lookup/run", json={"name": "Judge Dredd", "provider": "pdl", "mode": "work",
                                                "company": "Court"}, headers=ANN).status_code == 200
    # a donation to a police union PAC doesn't make someone protected; the name is never client-chosen
    mg = client.get("/api/lookup/person", params={"name": "Mark Greene (Acme Corp)"}, headers=ANN).json()
    assert not mg["protected"] and mg["employers"] == ["Acme Corp"]   # label's employer first, school dropped
    r = client.post("/api/lookup/run", json={"name": "Mark Greene (Acme Corp)", "provider": "pdl", "mode": "work"},
                    headers=ANN)
    assert calls[-1][2]["name"] == "Mark Greene"
    assert client.get("/api/lookup/person", params={"name": "Acme Corp"}, headers=ANN).status_code == 404


def test_people_can_block_lookups_about_themselves(client, setup):
    _certify(client, ANN)
    _key(client, ANN)
    # Judy's account name matches her record: verified at once
    r = client.post("/api/lookup/block", json={"name": "Judy Ruiz", "scope": "personal"}, headers=JUDY).json()
    assert r["status"] == "verified"
    r = client.post("/api/lookup/run", json={"name": "Judy Ruiz", "provider": "pdl", "mode": "personal",
                                             "company": "Ruiz Bakery"}, headers=ANN)
    assert r.status_code == 403 and r.json()["code"] == "blocked"
    assert client.post("/api/lookup/run", json={"name": "Judy Ruiz", "provider": "pdl", "mode": "work",
                                                "company": "Ruiz Bakery"}, headers=ANN).status_code == 200
    # Ann blocks someone who isn't her: pending, enforced, and the site admin can reject it
    r = client.post("/api/lookup/block", json={"name": "Jane Doe", "scope": "all"}, headers=ANN).json()
    assert r["status"] == "pending"
    p = client.get("/api/lookup/person", params={"name": "Jane Doe"}, headers=ANN).json()
    assert p["work_blocked"] and p["links"]["work"] == []
    assert client.post("/api/lookup/run", json={"name": "Jane Doe", "provider": "pdl", "mode": "work",
                                                "company": "Acme"}, headers=ANN).status_code == 403
    pend = client.get("/api/lookup/status", headers=ADMIN).json()["pending_blocks"]
    bid = next(b["id"] for b in pend if b["node"] == "Jane Doe")
    assert client.post(f"/api/lookup/block/{bid}/decide", json={"approve": False}, headers=ANN).status_code == 403
    assert client.post(f"/api/lookup/block/{bid}/decide", json={"approve": False}, headers=ADMIN).status_code == 200
    assert client.post("/api/lookup/run", json={"name": "Jane Doe", "provider": "pdl", "mode": "work",
                                                "company": "Acme"}, headers=ANN).status_code == 200
    # Judy can turn her own block back off; Ann can't remove it
    mine = client.get("/api/lookup/status", headers=JUDY).json()["my_blocks"]
    assert client.delete(f"/api/lookup/block/{mine[0]['id']}", headers=ANN).status_code == 404
    assert client.delete(f"/api/lookup/block/{mine[0]['id']}", headers=JUDY).status_code == 200


def test_team_keys_follow_membership_and_daily_cap(client, setup, monkeypatch):
    monkeypatch.setenv("WORKSPACES_BETA", "all")
    monkeypatch.setattr(pf, "_ws_allow_through_access", lambda email: (True, "ok"))
    ws_id = client.post("/api/ws", json={"name": "Acme Lookup", "admin_email": "ann@acme.example"},
                        headers=ADMIN).json()["id"]
    assert client.post(f"/api/ws/{ws_id}/members", json={"email": "judy.ruiz@x.example"}, headers=ANN).json()["success"]
    for h in (ANN, JUDY, ADMIN):
        _certify(client, h)
    assert _key(client, JUDY, "hunter", "hunter-team-key-1", ws_id).status_code == 403    # members can't set it
    assert _key(client, ANN, "hunter", "hunter-team-key-1", ws_id).status_code == 200
    calls = setup
    monkeypatch.setattr(cl, "http", lambda m, u, p, h: (calls.append((m, u, p, h)) or (200, {"data": {"email": "j@acme.com"}})))
    body = {"name": "Jane Doe", "provider": "hunter", "mode": "work", "domain": "https://www.acme.com/about", "ws_id": ws_id}
    r = client.post("/api/lookup/run", json=body, headers=JUDY)
    assert r.status_code == 200, r.text
    assert calls[-1][3]["X-API-KEY"] == "hunter-team-key-1" and calls[-1][2]["domain"] == "acme.com"
    assert client.post("/api/lookup/run", json=body, headers=ADMIN).status_code == 404    # not a member
    assert client.post("/api/lookup/run", json={**body, "mode": "personal"}, headers=JUDY).status_code == 400
    acts = [e["action"] for e in client.get(f"/api/ws/{ws_id}/audit", headers=ANN).json()["events"]]
    assert "contact_lookup" in acts and "lookup_key_set" in acts
    monkeypatch.setitem(cl.DAILY_CAP, "work", 1)
    r = client.post("/api/lookup/run", json=body, headers=JUDY)
    assert r.status_code == 429 and r.json()["code"] == "cap"
