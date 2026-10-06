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
