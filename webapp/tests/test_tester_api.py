"""Tests for the 9 tester/feedback/legal-review API endpoints of the
sixdegrees.net pathfinder webapp (the "test department" + submission +
legal-review surface, as opposed to the 7 public pathfinder endpoints
covered by test_api.py).

Run with:
    cd /mnt/c/Users/johnk/graphfinder-clean && python -m pytest webapp/tests/test_tester_api.py -v --timeout=60

Isolation: none of these 9 endpoints touch the graph or search index --
they only read/write webapp/data/user_submissions.db,
webapp/data/legal_compliance.db, and webapp/data/ops_metrics.db, and two
of them (extract-proof, legal/trigger-review) make real outbound network
calls (Gemini API, and an arbitrary caller-supplied URL). So this suite:

  - Monkeypatches `pathfinder.DATA_DIR` to a fresh pytest tmp_path for the
    whole session, BEFORE the FastAPI startup event runs (startup() reads
    DATA_DIR at call time to init the sqlite schemas), so every read/write
    lands in a throwaway directory instead of the real webapp/data/ --
    mirroring the "never touch real production data" approach used for
    the state-legislature agent's tests and the DB write concern flagged
    in test_api.py's own session.
  - Mocks `requests.post` (Gemini, for /api/extract-proof) and
    `requests.get` (for /api/legal/trigger-review, which fetches whatever
    URL the caller supplies) per-test, so the suite never makes a real
    network call.

One confirmed bug is documented below rather than hidden: `register_obligation`
is imported in `trigger_legal_review()` but never actually called, so
`legal_obligations` stays empty forever and the endpoint's "no_change"
branch is unreachable dead code -- every request for any URL, including a
repeat request for the same URL with byte-identical content, reports
"change_detected"/"new_source". Verified directly (two calls, empty table)
before writing the assertion.
"""
import base64
import email
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from fastapi.testclient import TestClient  # noqa: E402
import pathfinder as pf  # noqa: E402

pytestmark = pytest.mark.timeout(30)


# --------------------------------------------------------------------------
# Session-scoped fixtures: isolated data dir + one shared TestClient.
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def tester_data_dir(tmp_path_factory):
    return tmp_path_factory.mktemp("tester_api_data")


@pytest.fixture(scope="session")
def client(tester_data_dir):
    mp = pytest.MonkeyPatch()
    # Must happen before TestClient's `with` block, since that's what
    # triggers the FastAPI startup event that initializes the sqlite
    # schemas at (what is then) DATA_DIR.
    mp.setattr(pf, "DATA_DIR", tester_data_dir)
    with TestClient(pf.app) as c:
        yield c
    mp.undo()


def _mock_gemini_response(claim: dict, status_code: int = 200):
    resp = MagicMock(status_code=status_code)
    resp.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": json.dumps(claim)}]}}]
    }
    return resp


# ==========================================================================
# 1. POST /api/extract-proof
# ==========================================================================

def test_extract_proof_text_too_short_returns_422(client):
    r = client.post("/api/extract-proof", json={"text": "short"})
    assert r.status_code == 422


def test_extract_proof_success(client):
    claim = {
        "subject": "Jane Doe",
        "predicate": "FAMILY",
        "object": "John Doe",
        "source_name": "U.S. Senate Confirmation Questionnaire",
        "source_url": "https://example.com/doc",
        "snippet": "Jane Doe is married to John Doe.",
    }
    with patch("requests.post", return_value=_mock_gemini_response(claim)):
        r = client.post(
            "/api/extract-proof",
            json={"text": "This is a sufficiently long chat transcript proving a connection between two people."},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["claim"] == claim


def test_extract_proof_gemini_error_returns_500(client):
    bad_resp = MagicMock(status_code=500, text="upstream failure")
    with patch("requests.post", return_value=bad_resp):
        r = client.post(
            "/api/extract-proof",
            json={"text": "This is a sufficiently long chat transcript proving a connection between two people."},
        )
    assert r.status_code == 500
    body = r.json()
    assert body["success"] is False
    assert "error" in body


# ==========================================================================
# 2. POST /api/test-department/invite-bcc
# ==========================================================================

def test_invite_bcc_success(client):
    r = client.post(
        "/api/test-department/invite-bcc",
        json={"invitee_email": "invitee1@example.com", "invitee_name": "Invitee One"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert isinstance(body["event_id"], int)


def test_invite_bcc_missing_required_field_returns_422(client):
    r = client.post("/api/test-department/invite-bcc", json={})
    assert r.status_code == 422


# ==========================================================================
# 3. POST /api/test-department/feedback
# ==========================================================================

def test_feedback_success(client):
    r = client.post(
        "/api/test-department/feedback",
        json={"tester_email": "feedback1@example.com", "feedback_text": "The search results were great and helpful."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert isinstance(body["feedback_id"], int)


def test_feedback_missing_required_field_returns_422(client):
    r = client.post("/api/test-department/feedback", json={"tester_email": "x@example.com"})
    assert r.status_code == 422


# ==========================================================================
# 4. POST /api/test-department/note
# ==========================================================================

def test_note_success(client):
    r = client.post(
        "/api/test-department/note",
        json={"tester_email": "feedback1@example.com", "note_text": "Called them, everything resolved fine."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert isinstance(body["note_id"], int)


def test_note_text_too_short_returns_422(client):
    r = client.post(
        "/api/test-department/note",
        json={"tester_email": "feedback1@example.com", "note_text": "a"},
    )
    assert r.status_code == 422


# ==========================================================================
# 5. GET /api/test-department/testers/{tester_email}
# ==========================================================================

def test_tester_snapshot_known_tester(client):
    # feedback1@example.com already has feedback + a note from tests above,
    # via feedback_text containing "great and helpful" (classifies as
    # "praise", not one of CASE_OPEN_CATEGORIES) -- so this exercises the
    # non-blocked rollup path, distinct from the "bug" case tested via
    # test_summary_reflects_open_case below.
    r = client.get("/api/test-department/testers/feedback1@example.com")
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == "feedback1@example.com"
    assert body["feedback_count"] >= 1
    assert body["note_count"] >= 1
    assert "status" in body
    assert "recommended_next_action" in body


def test_tester_snapshot_unknown_tester_returns_404(client):
    r = client.get("/api/test-department/testers/nobody-at-all@example.com")
    assert r.status_code == 404
    assert r.json() == {"error": "tester not found"}


# ==========================================================================
# 6. GET /api/test-department/summary
# ==========================================================================

def test_summary_reflects_open_case_from_bug_feedback(client):
    # A feedback report containing a bug-classified keyword opens a case
    # and flips that tester's status to "blocked" (see classify_feedback /
    # CASE_OPEN_CATEGORIES / _derive_rollup in test_department.py).
    client.post(
        "/api/test-department/feedback",
        json={"tester_email": "buggy-tester@example.com", "feedback_text": "The app crashed with an error on load."},
    )
    r = client.get("/api/test-department/summary")
    assert r.status_code == 200
    body = r.json()
    assert "counts" in body and "total" in body and "queue" in body
    assert body["total"] >= 1
    assert body["counts"].get("blocked", 0) >= 1
    assert any(item["email"] == "buggy-tester@example.com" and item["status"] == "blocked" for item in body["queue"])


# ==========================================================================
# 6b. Operator notification on high-priority feedback
#
# Previously nothing alerted the operator at all when a bug/performance/
# trust/confusion/coverage report came in -- the queue had to be polled
# manually via /api/test-department/summary. This is the fix.
# ==========================================================================

def test_high_priority_feedback_notifies_operator(client):
    with patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp
        with patch.dict("os.environ", {
            "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
            "SMTP_USER": "testuser@gmail.com", "SMTP_PASS": "testpass",
            "OPERATOR_EMAIL": "operator@example.com",
        }):
            r = client.post(
                "/api/test-department/feedback",
                json={"tester_email": "buggy-tester2@example.com", "feedback_text": "The app crashed with an error on load."},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["category"] == "bug"
            assert body["operator_notified"] is True
            mock_smtp.sendmail.assert_called_once()
            from_addr, to_addrs, _msg = mock_smtp.sendmail.call_args[0]
            assert to_addrs == ["operator@example.com"], (
                "must notify the operator's address, not the submitting tester's"
            )


def test_normal_priority_feedback_does_not_notify_operator(client):
    with patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp
        with patch.dict("os.environ", {
            "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
            "SMTP_USER": "testuser@gmail.com", "SMTP_PASS": "testpass",
            "OPERATOR_EMAIL": "operator@example.com",
        }):
            r = client.post(
                "/api/test-department/feedback",
                json={"tester_email": "happy-tester@example.com", "feedback_text": "The search results were great and helpful."},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["category"] == "praise"
            assert body["operator_notified"] is False
            mock_smtp.sendmail.assert_not_called()


def test_high_priority_feedback_includes_automated_diagnosis_when_available(client):
    diagnosis_text = "Likely root cause: data-coverage gap. Confidence: Medium. Suggested next step: check whether the entity exists in the graph."
    fake_gemini_response = MagicMock(status_code=200)
    fake_gemini_response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": diagnosis_text}]}}]
    }
    with patch("smtplib.SMTP") as mock_smtp_class, patch("requests.post", return_value=fake_gemini_response):
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp
        with patch.dict("os.environ", {
            "SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587",
            "SMTP_USER": "testuser@gmail.com", "SMTP_PASS": "testpass",
            "OPERATOR_EMAIL": "operator@example.com",
            "GOOGLE_API_KEY": "fake-test-key",
        }):
            r = client.post(
                "/api/test-department/feedback",
                json={"tester_email": "buggy-tester4@example.com", "feedback_text": "I can't find a well-known senator anywhere in the search results."},
            )
            assert r.status_code == 200
            assert r.json()["operator_notified"] is True
            mock_smtp.sendmail.assert_called_once()
            _from_addr, _to_addrs, sent_message = mock_smtp.sendmail.call_args[0]
            # The message is base64-encoded per MIME part (Content-Transfer-
            # Encoding), so it must be parsed like a real email client would,
            # not substring-matched against the raw wire format.
            parsed = email.message_from_string(sent_message)
            decoded_parts = [part.get_payload(decode=True).decode("utf-8") for part in parsed.walk() if part.get_content_maintype() == "text"]
            assert any(diagnosis_text in part for part in decoded_parts), (
                "the automated diagnosis must actually be included in the operator email body"
            )


def test_high_priority_feedback_without_operator_email_configured_still_succeeds(client, monkeypatch):
    monkeypatch.delenv("OPERATOR_EMAIL", raising=False)
    r = client.post(
        "/api/test-department/feedback",
        json={"tester_email": "buggy-tester3@example.com", "feedback_text": "Getting a bug every time I search."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["operator_notified"] is False, (
        "no OPERATOR_EMAIL configured -- must degrade gracefully, not fail the feedback submission"
    )


# ==========================================================================
# 7. POST /api/legal/trigger-review
# ==========================================================================

def test_legal_trigger_review_new_url_reports_change_detected(client):
    fake_page = MagicMock(status_code=200, text="Terms of Service v1")
    fake_page.raise_for_status = lambda: None
    with patch("requests.get", return_value=fake_page):
        r = client.post("/api/legal/trigger-review", json={"source_url": "https://example.com/tos-fuzz-1"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["status"] == "change_detected"
    assert body["change"] == "new_source"


def test_legal_trigger_review_repeat_call_reports_no_change(client):
    """FIXED: register_obligation() now saves after first call, so a byte-identical
    repeat request correctly reports 'no_change'."""
    fake_page = MagicMock(status_code=200, text="Terms of Service, unchanged")
    fake_page.raise_for_status = lambda: None
    url = "https://example.com/tos-fuzz-2"
    with patch("requests.get", return_value=fake_page):
        r1 = client.post("/api/legal/trigger-review", json={"source_url": url})
        r2 = client.post("/api/legal/trigger-review", json={"source_url": url})
    assert r1.json() == {"success": True, "status": "change_detected", "change": "new_source"}
    assert r2.json() == {"success": True, "status": "no_change"}


def test_legal_trigger_review_fetch_failure_returns_500(client):
    with patch("requests.get", side_effect=Exception("connection refused")):
        r = client.post("/api/legal/trigger-review", json={"source_url": "https://example.com/unreachable"})
    assert r.status_code == 500
    assert "error" in r.json()


# ==========================================================================
# 8. POST /api/suggest-link
# ==========================================================================

def test_suggest_link_success_and_persists_row(client, tester_data_dir):
    r = client.post(
        "/api/suggest-link",
        json={
            "subject": "Jane Doe",
            "predicate": "FAMILY",
            "object": "John Doe",
            "source_name": "Wikipedia",
            "source_url": "http://example.com/bio",
            "snippet": "They are described as siblings.",
            "email": "tester@example.com",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True

    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    rows = conn.execute("SELECT subject, predicate, object FROM claims WHERE subject = 'Jane Doe'").fetchall()
    conn.close()
    assert ("Jane Doe", "FAMILY", "John Doe") in rows


def test_suggest_link_missing_required_field_returns_422(client):
    r = client.post("/api/suggest-link", json={"subject": "Jane Doe"})
    assert r.status_code == 422


# ==========================================================================
# 9. POST /api/dispute-link
# ==========================================================================

def test_dispute_link_success_and_persists_row(client, tester_data_dir):
    r = client.post(
        "/api/dispute-link",
        json={"edge_key": "jane doe|john doe|FAMILY", "reason": "These two people are not actually related."},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True

    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    rows = conn.execute("SELECT edge_key, reason FROM disputes WHERE edge_key = 'jane doe|john doe|FAMILY'").fetchall()
    conn.close()
    assert len(rows) == 1


def test_dispute_link_missing_required_field_returns_422(client):
    r = client.post("/api/dispute-link", json={})
    assert r.status_code == 422


# ==========================================================================
# 9b. POST /api/contacts/add-me ("Check My Contacts" -> "Add Me")
# ==========================================================================

def test_add_me_without_auth_header_returns_401(client):
    r = client.post("/api/contacts/add-me", json={"person_name": "Jane Doe"})
    assert r.status_code == 401
    assert r.json()["success"] is False


def test_add_me_missing_person_name_returns_422(client):
    r = client.post(
        "/api/contacts/add-me", json={},
        headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
    )
    assert r.status_code == 422


def test_add_me_unresolvable_person_returns_400(client):
    with patch("pathfinder._resolve_name", return_value=None):
        r = client.post(
            "/api/contacts/add-me", json={"person_name": "Nobody Real"},
            headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
        )
    assert r.status_code == 400
    assert r.json()["success"] is False


def test_add_me_loads_graph_before_resolving_name(client):
    """Regression test: _resolve_name() depends on the module-level _graph,
    which only loads lazily on the first path search (cold-start perf) --
    confirmed live that add-me called _resolve_name() without first calling
    _load_graph(), so a fresh server process incorrectly reported real,
    valid people as 'not found in graph' until someone happened to run a
    path search first."""
    with patch("pathfinder._resolve_name", side_effect=lambda x: x), \
         patch("pathfinder._load_graph") as mock_load_graph:
        r = client.post(
            "/api/contacts/add-me", json={"person_name": "Jane Doe"},
            headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
        )
    assert r.status_code == 200
    mock_load_graph.assert_called_once()


def test_add_me_success_persists_self_attested_suggestion_not_spoofable_by_body(client, tester_data_dir):
    with patch("pathfinder._resolve_name", side_effect=lambda x: x):
        r = client.post(
            "/api/contacts/add-me",
            json={"person_name": "Jane Doe", "email": "attacker@evil.com"},
            headers={"Cf-Access-Authenticated-User-Email": "alice@example.com"},
        )
    assert r.status_code == 200
    assert r.json()["success"] is True

    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM service_items WHERE item_type = 'suggestion' AND submitter_email = 'alice@example.com' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None
    # AddMeRequest has no `email` field, so a spoofed body value is simply
    # ignored -- attribution comes only from the Cf-Access header.
    assert row["submitter_email"] == "alice@example.com"
    meta = json.loads(row["metadata"])
    assert meta["predicate"] == "SELF_ATTESTED_CONTACT"
    assert meta["object"] == "Jane Doe"
    assert meta["source_name"] == "Self-reported by alice@example.com"


def test_add_me_approval_creates_relationship_without_subject_preexisting(client, tester_data_dir):
    """Regression test for the approval-gate patch: a SELF_ATTESTED_CONTACT
    suggestion's subject is the reporting user's own name, which is expected
    to NOT already resolve as a graph node (unlike ordinary suggestions,
    where both endpoints must pre-exist). Approval must still succeed and
    write a pipeline_cache relationship row using the object alone."""
    with patch("pathfinder._resolve_name", side_effect=lambda x: x):
        add_res = client.post(
            "/api/contacts/add-me", json={"person_name": "Jane Doe"},
            headers={"Cf-Access-Authenticated-User-Email": "bob@example.com"},
        )
    assert add_res.status_code == 200

    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    conn.row_factory = sqlite3.Row
    item = conn.execute(
        "SELECT id FROM service_items WHERE item_type = 'suggestion' AND submitter_email = 'bob@example.com' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    item_id = item["id"]

    pipeline_db = tester_data_dir / "pipeline_cache_addme.db"
    conn = sqlite3.connect(str(pipeline_db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            source_id TEXT, source_name TEXT, source_type TEXT,
            target_id TEXT, target_name TEXT, target_type TEXT,
            relation_type TEXT, source_data TEXT, evidence TEXT
        )
    """)
    conn.close()

    # Only the object ("Jane Doe") resolves -- the subject (the reporting
    # user's own display name) does not, simulating a brand-new person.
    with patch("pathfinder.send_email", return_value={"success": True, "message_id": "<test-msg-id>"}), \
         patch("pathfinder._resolve_name", side_effect=lambda x: x if x == "Jane Doe" else None), \
         patch("pathfinder._get_pipeline_db_path", return_value=str(pipeline_db)):
        rev_res = client.post(f"/api/service/items/{item_id}/review", json={
            "status": "approved", "reviewed_by": "reviewer@example.com",
        })
    assert rev_res.status_code == 200
    assert rev_res.json()["success"] is True

    conn = sqlite3.connect(str(pipeline_db))
    rel = conn.execute("SELECT * FROM relationships").fetchone()
    conn.close()
    assert rel is not None
    assert rel[4] == "Jane Doe"              # target_name
    assert rel[5] == "PERSON"                # target_type
    assert rel[6] == "SELF_ATTESTED_CONTACT"  # relation_type
    assert rel[7] == "USER_SUGGESTION"        # source_data


# ==========================================================================
# 9c. Cf-Access-Jwt-Assertion fallback (this deployment's Cloudflare Access
# config forwards the JWT assertion but not the Cf-Access-Authenticated-
# User-Email convenience header -- confirmed live via a temporary header-echo
# diagnostic -- so _request_user_email() must verify and decode the JWT).
# ==========================================================================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_test_cf_access_jwt(email, private_key, kid="test-kid-1", exp_delta=3600):
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    import time as _time
    header_b64 = _b64url(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    payload_b64 = _b64url(json.dumps({"email": email, "exp": int(_time.time()) + exp_delta}).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode()
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url(signature)}"


def _jwk_from_public_key(pubkey, kid):
    numbers = pubkey.public_numbers()
    def b64url_uint(x):
        return _b64url(x.to_bytes((x.bit_length() + 7) // 8, "big"))
    return {"kid": kid, "kty": "RSA", "alg": "RS256", "n": b64url_uint(numbers.n), "e": b64url_uint(numbers.e)}


@pytest.fixture
def cf_access_keypair(monkeypatch):
    """Generates a throwaway RSA keypair, points pathfinder's JWKS fetch at a
    mocked response serving its public half, and resets the module's JWKS
    cache so each test starts clean."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    kid = "test-kid-1"
    jwks_response = MagicMock(status_code=200)
    jwks_response.json.return_value = {"keys": [_jwk_from_public_key(private_key.public_key(), kid)]}
    jwks_response.raise_for_status = lambda: None
    pf._cf_access_jwks_cache["keys"] = {}
    pf._cf_access_jwks_cache["fetched_at"] = 0.0
    with patch("requests.get", return_value=jwks_response):
        yield private_key, kid
    pf._cf_access_jwks_cache["keys"] = {}
    pf._cf_access_jwks_cache["fetched_at"] = 0.0


def test_add_me_authenticates_via_cf_access_jwt_when_convenience_header_absent(client, tester_data_dir, cf_access_keypair):
    private_key, kid = cf_access_keypair
    token = _make_test_cf_access_jwt("carol@example.com", private_key, kid=kid)
    with patch("pathfinder._resolve_name", side_effect=lambda x: x):
        r = client.post(
            "/api/contacts/add-me", json={"person_name": "Jane Doe"},
            headers={"Cf-Access-Jwt-Assertion": token},
        )
    assert r.status_code == 200
    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM service_items WHERE item_type = 'suggestion' AND submitter_email = 'carol@example.com' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row is not None


def test_add_me_rejects_jwt_with_invalid_signature(client, cf_access_keypair):
    from cryptography.hazmat.primitives.asymmetric import rsa
    _, kid = cf_access_keypair
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged_token = _make_test_cf_access_jwt("mallory@example.com", wrong_key, kid=kid)
    with patch("pathfinder._resolve_name", side_effect=lambda x: x):
        r = client.post(
            "/api/contacts/add-me", json={"person_name": "Jane Doe"},
            headers={"Cf-Access-Jwt-Assertion": forged_token},
        )
    assert r.status_code == 401


def test_add_me_rejects_expired_jwt(client, cf_access_keypair):
    private_key, kid = cf_access_keypair
    expired_token = _make_test_cf_access_jwt("dave@example.com", private_key, kid=kid, exp_delta=-3600)
    with patch("pathfinder._resolve_name", side_effect=lambda x: x):
        r = client.post(
            "/api/contacts/add-me", json={"person_name": "Jane Doe"},
            headers={"Cf-Access-Jwt-Assertion": expired_token},
        )
    assert r.status_code == 401


# ==========================================================================
# 10. Service Department Unified Triage & Email tests (SPEC 1 & 2)
# ==========================================================================

def test_invite_bcc_triggers_smtp_send(client, tester_data_dir):
    with patch("smtplib.SMTP") as mock_smtp_class:
        mock_smtp = MagicMock()
        mock_smtp_class.return_value = mock_smtp
        
        with patch.dict("os.environ", {
            "SMTP_HOST": "smtp.gmail.com",
            "SMTP_PORT": "587",
            "SMTP_USER": "testuser@gmail.com",
            "SMTP_PASS": "testpass",
            "TESTER_INVITE_FROM": "tester-support@sixdegrees.net"
        }):
            r = client.post(
                "/api/test-department/invite-bcc",
                json={
                    "invitee_email": "invitee_smtp@example.com",
                    "invitee_name": "Invitee SMTP",
                    "inviter_email": "inviter@example.com",
                    "subject": "Join us",
                    "body": "Welcome"
                },
            )
            assert r.status_code == 200
            body = r.json()
            assert body["success"] is True
            assert body["delivery_status"] == "sent"
            assert "message_id" in body
            
            mock_smtp_class.assert_called_with("smtp.gmail.com", 587, timeout=10)
            mock_smtp.starttls.assert_called_once()
            mock_smtp.login.assert_called_with("testuser@gmail.com", "testpass")
            mock_smtp.sendmail.assert_called_once()
            mock_smtp.quit.assert_called_once()
            
            conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
            row = conn.execute("SELECT invite_delivery_status, invite_message_id FROM testers WHERE email = 'invitee_smtp@example.com'").fetchone()
            conn.close()
            assert row is not None
            assert row[0] == "sent"
            assert row[1] == body["message_id"]


def test_service_queue_and_review_workflow(client, tester_data_dir):
    r = client.get("/api/service/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    queue = body["queue"]
    
    suggestions = [item for item in queue if item["item_type"] == "suggestion"]
    disputes = [item for item in queue if item["item_type"] == "dispute"]
    assert len(suggestions) > 0
    assert len(disputes) > 0

    # Select by content, not position: other tests (e.g. add-me) also leave
    # 'new'-status suggestion items in this shared session-scoped DB, and
    # queue order isn't guaranteed to put this specific one first.
    s_item = next(
        item for item in suggestions
        if json.loads(item["metadata"]).get("subject") == "Jane Doe"
        and json.loads(item["metadata"]).get("object") == "John Doe"
    )
    s_id = s_item["id"]
    
    # Initialize mock pipeline_cache.db relationships table
    pipeline_db = tester_data_dir / "pipeline_cache.db"
    conn = sqlite3.connect(str(pipeline_db))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            source_id TEXT,
            source_name TEXT,
            source_type TEXT,
            target_id TEXT,
            target_name TEXT,
            target_type TEXT,
            relation_type TEXT,
            source_data TEXT,
            evidence TEXT
        )
    """)
    conn.close()
    
    with patch("pathfinder.send_email", return_value={"success": True, "message_id": "<test-msg-id>"}) as mock_send, \
         patch("pathfinder._resolve_name", side_effect=lambda x: x), \
         patch("pathfinder._get_pipeline_db_path", return_value=str(pipeline_db)):
         
        import networkx as nx
        pf._graph = nx.Graph()
        pf._graph.add_node("Jane Doe")
        pf._graph.add_node("John Doe")
        
        rev_res = client.post(f"/api/service/items/{s_id}/review", json={
            "status": "approved",
            "reviewed_by": "reviewer@example.com",
            "note": "Valid connection found"
        })
        assert rev_res.status_code == 200
        assert rev_res.json()["success"] is True
        mock_send.assert_called()
        
        conn = sqlite3.connect(str(tester_data_dir / "pipeline_cache.db"))
        rel = conn.execute("SELECT * FROM relationships").fetchone()
        conn.close()
        assert rel is not None
        assert rel[1] == "Jane Doe"
        assert rel[4] == "John Doe"
        assert rel[7] == "USER_SUGGESTION"


def test_service_review_loads_graph_before_resolving_names(client, tester_data_dir):
    """Regression test: the approval branch's _resolve_name() calls also
    depend on the lazily-loaded module-level _graph (only loads on the first
    path search otherwise) -- this call site had the same gap as add-me."""
    conn = sqlite3.connect(str(tester_data_dir / "test_department.db"))
    meta = json.dumps({
        "subject": "Regression Subject", "predicate": "FAMILY", "object": "Regression Object",
        "source_name": "Test", "source_url": "http://example.com", "snippet": "test",
    })
    conn.execute(
        "INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, metadata) "
        "VALUES ('suggestion', 'new', 'normal', ?, ?, ?, ?)",
        ("Regression test item", "body", "tester@example.com", meta),
    )
    conn.commit()
    item_id = conn.execute("SELECT id FROM service_items WHERE subject = 'Regression test item'").fetchone()[0]
    conn.close()

    with patch("pathfinder.send_email", return_value={"success": True, "message_id": "<test-msg-id>"}), \
         patch("pathfinder._resolve_name", side_effect=lambda x: x), \
         patch("pathfinder._load_graph") as mock_load_graph:
        r = client.post(f"/api/service/items/{item_id}/review", json={
            "status": "approved", "reviewed_by": "reviewer@example.com",
        })
    assert r.status_code == 200
    mock_load_graph.assert_called_once()


def test_service_metrics(client):
    r = client.get("/api/service/metrics")
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["success"] is True
    assert "metrics" in body
    metrics = body["metrics"]
    assert "approval_rate" in metrics
    assert "avg_resolution_time_seconds" in metrics
    assert "testers" in metrics


def test_manual_notification(client):
    r = client.get("/api/service/queue")
    queue = r.json()["queue"]
    item_with_email = [i for i in queue if i["submitter_email"] is not None]
    if item_with_email:
        i_id = item_with_email[0]["id"]
        with patch("pathfinder.send_email", return_value={"success": True, "message_id": "<test-msg-id>"}) as mock_send:
            res = client.post(f"/api/service/notify/{i_id}")
            assert res.status_code == 200
            assert res.json()["success"] is True
            mock_send.assert_called_once()

