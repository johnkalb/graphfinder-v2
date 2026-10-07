"""HTTP layer for each user's private/public links (/api/my/*), the agreement
gate, Add Me in the beta, the sync export and live filtering of deleted public links."""
from unittest.mock import patch

import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)
USER = {"Cf-Access-Authenticated-User-Email": "beta.user@example.com"}


@pytest.fixture(autouse=True)
def beta_on(monkeypatch):
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "all")
    monkeypatch.setattr(pf, "_resolve_name", lambda x: x)
    monkeypatch.setattr(pf, "_load_demo_verified", lambda: {"barack obama"})
    pf._invalidate_removed_links()
    yield


def test_requires_login_and_beta_flag(client, monkeypatch):
    assert client.get("/api/my/links").status_code == 401
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "admins")
    assert client.get("/api/my/links", headers=USER).status_code == 404


def test_agreement_gate_then_full_lifecycle(client):
    r = client.post("/api/my/links", json={"contact": "Jane Doe"}, headers=USER)
    assert r.status_code == 428 and r.json()["error"] == "agreement_required"
    assert client.post("/api/agreement/accept", headers=USER).json()["success"]

    r = client.post("/api/my/links", json={"contact": "Jane Doe", "source": "linkedin"}, headers=USER)
    assert r.status_code == 200
    jane = r.json()["link"]
    assert jane["visibility"] == "private" and jane["relation"] == "LINKEDIN_CONNECTION"

    r = client.post("/api/my/links", json={"contact": "Jane Doe Two", "visibility": "public"}, headers=USER)
    assert r.status_code == 400 and "verified public figures" in r.json()["error"]

    r = client.post("/api/my/links", json={"contact": "Barack Obama", "visibility": "public"}, headers=USER)
    assert r.status_code == 200 and r.json()["link"]["visibility"] == "public"
    obama = r.json()["link"]

    listing = client.get("/api/my/links", headers=USER).json()
    assert {l["contact"] for l in listing["links"]} == {"Jane Doe", "Barack Obama"}
    assert listing["public_count"] == 1 and listing["agreement_accepted"] is True

    export = client.get("/api/my/links/export", headers=USER)
    assert export.status_code == 200 and "Barack Obama" in export.text

    assert client.delete(f"/api/my/links/{obama['id']}", headers=USER).json()["success"]
    assert client.delete(f"/api/my/links/{obama['id']}", headers=USER).status_code == 404
    assert {l["contact"] for l in client.get("/api/my/links", headers=USER).json()["links"]} == {"Jane Doe"}


def test_other_users_cannot_see_or_delete(client):
    client.post("/api/agreement/accept", headers=USER)
    link = client.post("/api/my/links", json={"contact": "Private Pal"}, headers=USER).json()["link"]
    other = {"Cf-Access-Authenticated-User-Email": "someone.else@example.com"}
    assert "Private Pal" not in str(client.get("/api/my/links", headers=other).json())
    assert client.delete(f"/api/my/links/{link['id']}", headers=other).status_code == 404


def test_add_me_in_the_beta_makes_a_private_link_not_a_review_item(client):
    client.post("/api/agreement/accept", headers=USER)
    r = client.post("/api/contacts/add-me", json={"person_name": "Addme Person", "source": "linkedin"}, headers=USER)
    assert r.status_code == 200 and r.json()["private_link"] is True
    assert "Addme Person" in {l["contact"] for l in client.get("/api/my/links", headers=USER).json()["links"]}


def test_sync_export_lists_public_and_removed_links(client, monkeypatch):
    client.post("/api/agreement/accept", headers=USER)
    pub = client.post("/api/my/links", json={"contact": "Barack Obama", "visibility": "public"}, headers=USER).json()["link"]
    monkeypatch.setenv("USER_LINKS_SYNC_TOKEN", "tok")
    body = client.get("/api/internal/user-links", headers={"X-Sync-Token": "tok"}).json()
    assert {"owner": "Beta User", "contact": "Barack Obama"}.items() <= next(
        l for l in body["public_links"] if l["contact"] == "Barack Obama").items()
    client.delete(f"/api/my/links/{pub['id']}", headers=USER)
    body = client.get("/api/internal/user-links", headers={"X-Sync-Token": "tok"}).json()
    assert any(l["contact"] == "Barack Obama" and l["owner"] == "Beta User" for l in body["removed_links"])
    assert not any(l["contact"] == "Barack Obama" and l["owner"] == "Beta User" for l in body["public_links"])


def test_deleted_public_links_are_dropped_from_live_paths(client):
    client.post("/api/agreement/accept", headers=USER)
    pub = client.post("/api/my/links", json={"contact": "Barack Obama", "visibility": "public"}, headers=USER).json()["link"]
    client.delete(f"/api/my/links/{pub['id']}", headers=USER)
    pf._invalidate_removed_links()
    res = {"paths": [
        {"path": [{"node": "Beta User"}, {"node": "Barack Obama"}, {"node": "Chuck Schumer"}]},
        {"path": [{"node": "Someone"}, {"node": "Barack Obama"}]},
    ]}
    out = pf._drop_removed_link_paths(res)
    assert [p["path"][0]["node"] for p in out["paths"]] == ["Someone"]


def test_report_goes_to_the_review_queue(client, tester_data_dir):
    import sqlite3
    r = client.post("/api/links/report", json={"subject": "Beta User", "contact": "Barack Obama", "reason": "not true"},
                    headers={"Cf-Access-Authenticated-User-Email": "reporter@example.com"})
    assert r.status_code == 200
    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    n = conn.execute("SELECT count(*) FROM service_items WHERE item_type = 'link_report'").fetchone()[0]
    conn.close()
    assert n >= 1


def test_terms_page_and_panel_markup(client):
    r = client.get("/terms")
    assert r.status_code == 200 and "indemnify" in r.text and "not a subscription" in r.text
    page = client.get("/").text
    assert 'id="mylinks-section"' in page and "Beta &mdash; free while we test; not a subscription" in page


def test_reach_routes_through_the_users_own_contacts(monkeypatch):
    """Tiny graph: Alice - Bob - Target, and Carol - Dave - Erin - Target.
    The user knows Alice and Carol (private links). Best route: You -> Alice -> Bob -> Target."""
    import math
    import numpy as np
    import igraph as ig
    names = ["Alice", "Bob", "Target", "Carol", "Dave", "Erin"]
    edges = [(0, 1), (1, 2), (3, 4), (4, 5), (5, 2)]
    probs = np.array([0.8] * len(edges))
    fwd = -math.log(pf._FORWARD_PROB)
    w = -np.log(probs) + fwd
    g = ig.Graph(n=len(names), edges=edges)
    for attr, val in {"_igraph_graph": g, "_igraph_nodes": names,
                      "_igraph_name_to_idx": {n: i for i, n in enumerate(names)},
                      "_igraph_weight": w, "_igraph_living_weight": w, "_igraph_prob": probs,
                      "_igraph_cats_mask": np.zeros(len(edges), dtype=np.uint32), "_igraph_cats_vocab": [],
                      "_igraph_deceased_idx": set()}.items():
        monkeypatch.setattr(pf, attr, val)
    monkeypatch.setattr(pf, "_load_igraph", lambda: None)
    monkeypatch.setattr(pf, "_load_deceased", lambda: {})
    monkeypatch.setattr(pf, "_get_label", lambda n: n)
    monkeypatch.setattr(pf, "_get_node_sci", lambda n: 50)
    contacts = [{"contact": "Alice", "visibility": "private"}, {"contact": "Carol", "visibility": "private"}]
    res = pf._reach_via_contacts("Test User", contacts, "Target", k=2)
    assert [s["node"] for s in res["paths"][0]["path"]] == ["Test User", "Alice", "Bob", "Target"]
    assert res["paths"][0]["path"][0]["you"] is True and res["paths"][0]["path"][0]["prob"] == pf._OWNER_LINK_PROB
    assert res["paths"][0]["via_contact"] == "Alice"
    assert [s["node"] for s in res["paths"][1]["path"]][:2] == ["Test User", "Carol"]
    # a contact who IS the target: one step
    direct = pf._reach_via_contacts("Test User", [{"contact": "Target"}], "Target", k=1)
    assert [s["node"] for s in direct["paths"][0]["path"]] == ["Test User", "Target"]
    # an UNMATCHED contact has no node: "Alice" here is a namesake, not the user's Alice
    unmatched = pf._reach_via_contacts("Test User", [{"contact": "Alice", "contact_type": "UNMATCHED"}], "Target", k=1)
    assert unmatched["paths"] == []


def test_none_of_these_and_wrong_person(client):
    client.post("/api/agreement/accept", headers=USER)
    r = client.post("/api/my/links/unmatched", json={"contact": "Mark Greene", "source": "linkedin"}, headers=USER)
    assert r.status_code == 200 and r.json()["link"]["contact_type"] == "UNMATCHED"
    pub = client.post("/api/my/links", json={"contact": "Barack Obama", "visibility": "public"},
                      headers=USER).json()["link"]
    r = client.post(f"/api/my/links/{pub['id']}/wrong-person", headers=USER)
    assert r.status_code == 200 and r.json()["link"]["contact_type"] == "UNMATCHED"
    links = client.get("/api/my/links", headers=USER).json()["links"]
    pairs = {(l["contact"], l["contact_type"]) for l in links}   # the DB is shared across this module's tests
    assert {("Mark Greene", "UNMATCHED"), ("Barack Obama", "UNMATCHED")} <= pairs
    assert ("Barack Obama", "PERSON") not in pairs
    other = {"Cf-Access-Authenticated-User-Email": "someone.else@example.com"}
    assert client.post(f"/api/my/links/{pub['id']}/wrong-person", headers=other).status_code == 404
