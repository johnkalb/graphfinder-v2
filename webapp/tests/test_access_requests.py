"""Public access requests (/request-access) + Cloudflare allow-list approval,
and the auth hardening that shipped with them (2026-09-25).

Reuses test_tester_api.py's session client/data-dir and JWT helpers.
"""
import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from test_tester_api import (  # noqa: F401 -- fixtures are used by name
    ADMIN_HEADERS, _make_test_cf_access_jwt, cf_access_keypair, client, pf, tester_data_dir,
)

pytestmark = pytest.mark.timeout(30)


def _access_rows(tester_data_dir, email):
    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM service_items WHERE item_type = 'access_request' AND submitter_email = ?",
                        (email,)).fetchall()
    conn.close()
    return rows


@pytest.fixture(autouse=True)
def fresh_rate_limit():
    pf._access_request_hits.clear()
    yield
    pf._access_request_hits.clear()


def _submit_and_get_id(client, tester_data_dir, email):
    with patch("pathfinder._notify_operator_of_new_submission"):
        client.post("/request-access/submit", json={"email": email})
    return _access_rows(tester_data_dir, email)[0]["id"]


# --- the public page and submit endpoint -----------------------------------

def test_request_access_page_is_served(client):
    r = client.get("/request-access")
    assert r.status_code == 200
    assert "Request access" in r.text
    assert "__TURNSTILE" not in r.text  # placeholders always substituted


def test_request_access_page_includes_turnstile_when_configured(client, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SITE_KEY", "site-key-123")
    r = client.get("/request-access")
    assert 'data-sitekey="site-key-123"' in r.text
    assert "challenges.cloudflare.com/turnstile" in r.text


def test_submit_stores_request(client, tester_data_dir):
    with patch("pathfinder._notify_operator_of_new_submission") as notify:
        r = client.post("/request-access/submit",
                        json={"email": "Fedi.User@Example.org", "handle": "fedi@mastodon.social",
                              "note": "saw the crawlie"})
    assert r.status_code == 200 and r.json()["success"] is True
    rows = _access_rows(tester_data_dir, "fedi.user@example.org")
    assert len(rows) == 1 and rows[0]["status"] == "new"
    assert json.loads(rows[0]["metadata"])["handle"] == "@fedi@mastodon.social"
    notify.assert_called_once()


def test_duplicate_request_not_stored_twice(client, tester_data_dir):
    with patch("pathfinder._notify_operator_of_new_submission"):
        for _ in range(2):
            assert client.post("/request-access/submit", json={"email": "twice@example.org"}).status_code == 200
    assert len(_access_rows(tester_data_dir, "twice@example.org")) == 1


def test_honeypot_stores_nothing(client, tester_data_dir):
    r = client.post("/request-access/submit", json={"email": "bot@example.org", "website": "http://spam"})
    assert r.status_code == 200 and r.json()["success"] is True
    assert _access_rows(tester_data_dir, "bot@example.org") == []


def test_rejects_bad_email_and_handle(client):
    assert client.post("/request-access/submit", json={"email": "not-an-email"}).status_code == 400
    assert client.post("/request-access/submit",
                       json={"email": "ok@example.org", "handle": "no-server"}).status_code == 400


def test_rate_limited_per_ip(client):
    limit = pf._ACCESS_REQUEST_LIMIT_PER_HOUR
    with patch("pathfinder._notify_operator_of_new_submission"):
        codes = [client.post("/request-access/submit", json={"email": f"rl{i}@example.org"}).status_code
                 for i in range(limit + 1)]
    assert codes[:-1] == [200] * limit and codes[-1] == 429


def test_turnstile_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("TURNSTILE_SECRET_KEY", "test-secret")
    assert client.post("/request-access/submit", json={"email": "noturnstile@example.org"}).status_code == 400


# --- admin queue + approval --------------------------------------------------

def test_service_queue_requires_admin(client):
    assert client.get("/api/service/queue").status_code == 403
    assert client.get("/api/service/queue",
                      headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"}).status_code == 403


def test_approving_adds_email_to_cloudflare(client, tester_data_dir):
    item_id = _submit_and_get_id(client, tester_data_dir, "approve-me@example.org")
    with patch("pathfinder._cf_access_allow_email", return_value=(True, "added to the allow-list")) as cf:
        r = client.post(f"/api/service/items/{item_id}/review", json={"status": "approved"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    cf.assert_called_once_with("approve-me@example.org")
    row = _access_rows(tester_data_dir, "approve-me@example.org")[0]
    assert row["status"] == "approved" and "allow-list" in row["resolution_note"]


def test_stays_pending_when_cloudflare_fails(client, tester_data_dir):
    item_id = _submit_and_get_id(client, tester_data_dir, "cf-down@example.org")
    with patch("pathfinder._cf_access_allow_email", return_value=(False, "boom")):
        r = client.post(f"/api/service/items/{item_id}/review", json={"status": "approved"}, headers=ADMIN_HEADERS)
    assert r.status_code == 424
    assert "boom" in r.json()["error"]
    assert _access_rows(tester_data_dir, "cf-down@example.org")[0]["status"] == "new"


def test_rejecting_does_not_touch_cloudflare(client, tester_data_dir):
    item_id = _submit_and_get_id(client, tester_data_dir, "reject-me@example.org")
    with patch("pathfinder._cf_access_allow_email") as cf:
        r = client.post(f"/api/service/items/{item_id}/review", json={"status": "rejected"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    cf.assert_not_called()


def test_non_admin_cannot_approve(client, tester_data_dir):
    item_id = _submit_and_get_id(client, tester_data_dir, "sneaky@example.org")
    with patch("pathfinder._cf_access_allow_email") as cf:
        r = client.post(f"/api/service/items/{item_id}/review", json={"status": "approved"},
                        headers={"Cf-Access-Authenticated-User-Email": "sneaky@example.org"})
    assert r.status_code == 403
    cf.assert_not_called()


def test_cf_allow_email_preserves_policy_and_appends(monkeypatch):
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a")
    existing = {"name": "Allowed_Users", "decision": "allow", "include": [{"email": {"email": "old@example.org"}}],
                "exclude": [], "require": [], "id": "x"}
    get_resp = MagicMock()
    get_resp.json.return_value = {"success": True, "result": existing}
    put_resp = MagicMock()
    put_resp.json.return_value = {"success": True, "result": {}}
    with patch("requests.get", return_value=get_resp), patch("requests.put", return_value=put_resp) as put:
        ok, _ = pf._cf_access_allow_email("new@example.org")
    assert ok
    body = put.call_args.kwargs["json"]
    assert body["name"] == "Allowed_Users" and body["decision"] == "allow"
    assert body["include"] == [{"email": {"email": "old@example.org"}}, {"email": {"email": "new@example.org"}}]
    with patch("requests.get", return_value=get_resp), patch("requests.put") as put2:
        ok, msg = pf._cf_access_allow_email("old@example.org")
    assert ok and "already" in msg
    put2.assert_not_called()


def test_cf_allow_email_without_credentials_fails_cleanly(monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    ok, msg = pf._cf_access_allow_email("x@example.org")
    assert not ok and "not configured" in msg


# --- auth hardening -----------------------------------------------------------

def test_plain_email_header_ignored_in_production(client, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TRUST_EMAIL_HEADER", raising=False)
    assert client.get("/api/service/queue", headers=ADMIN_HEADERS).status_code == 403


def test_jwt_for_another_access_app_is_rejected(client, cf_access_keypair):
    private_key, kid = cf_access_keypair
    wrong = _make_test_cf_access_jwt("john.kalb@gmail.com", private_key, kid=kid, aud="some-other-app-aud")
    assert client.get("/api/service/queue", headers={"Cf-Access-Jwt-Assertion": wrong}).status_code == 403
    good = _make_test_cf_access_jwt("john.kalb@gmail.com", private_key, kid=kid)
    assert client.get("/api/service/queue", headers={"Cf-Access-Jwt-Assertion": good}).status_code == 200
