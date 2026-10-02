"""Public demo (/demo, /api/demo/*): verified public figures only, private
people on a path anonymised, rate limited (2026-10-01).

Reuses test_tester_api.py's session client.
"""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)

VERIFIED = {"meryl streep", "chuck schumer", "warren buffett"}


def _step(node, relation=None):
    return {"node": node, "label": node, "relation": relation, "prob": 0.8, "cats": [relation] if relation else [],
            "deceased": None, "sci": 50}


FAKE_RESULT = {"paths": [{"length": 3, "band": "Plausible", "probability": 0.2, "path": [
    _step("Meryl Streep", "DONATION"),
    _step("Jane Q Donor", "DONATION"),           # private person: must be anonymised
    _step("Democratic Senatorial Campaign Committee", "PUBLIC_OFFICE"),
    _step("Chuck Schumer"),
]}], "src_found": True, "tgt_found": True}


@pytest.fixture(autouse=True)
def demo_env(monkeypatch):
    monkeypatch.setattr(pf, "_load_demo_verified", lambda: VERIFIED)
    monkeypatch.setattr(pf, "_graph_backend_ready", lambda: True)
    monkeypatch.setattr(pf, "_path_process_pool", None)
    monkeypatch.setattr(pf, "_find_path_dispatch", lambda *a, **k: FAKE_RESULT)
    monkeypatch.setattr(pf, "_find_entry", lambda q: [
        {"canonical": "Meryl Streep", "degree": 300}, {"canonical": "Meryl Smith", "degree": 4},
        {"canonical": "Merrill Lynch", "degree": 900}])
    pf._demo_hits.clear()
    pf._demo_global_hits.clear()
    yield
    pf._demo_hits.clear()
    pf._demo_global_hits.clear()


def test_demo_page_is_served(client):
    r = client.get("/demo")
    assert r.status_code == 200
    assert "How are they connected?" in r.text
    assert "/request-access?from=demo" in r.text


def test_search_suggests_only_verified_people(client):
    r = client.get("/api/demo/search", params={"q": "mer"})
    assert r.json() == [{"name": "Meryl Streep", "degree": 300}]


def test_path_requires_verified_endpoints(client):
    r = client.get("/api/demo/path", params={"src_name": "Meryl Streep", "tgt_name": "Jane Q Donor"})
    assert r.status_code == 400
    assert r.json()["error"] == "not_public_figure"


def test_path_anonymises_private_people_and_strips_details(client):
    r = client.get("/api/demo/path", params={"src_name": "meryl streep", "tgt_name": "Chuck Schumer"})
    assert r.status_code == 200
    steps = r.json()["paths"][0]["path"]
    assert [s["label"] for s in steps] == ["Meryl Streep", "A private individual",
                                           "Democratic Senatorial Campaign Committee", "Chuck Schumer"]
    assert steps[1]["node"] is None and steps[1]["private"] is True
    assert "Jane Q Donor" not in r.text
    assert "sci" not in steps[0] and "deceased" not in steps[0]
    assert "probability" not in r.json()["paths"][0]


def test_path_rate_limit_per_ip(client, monkeypatch):
    monkeypatch.setitem(pf._DEMO_LIMITS, "path", (2, 3600))
    params = {"src_name": "Meryl Streep", "tgt_name": "Chuck Schumer"}
    headers = {"CF-Connecting-IP": "203.0.113.9"}
    assert client.get("/api/demo/path", params=params, headers=headers).status_code == 200
    assert client.get("/api/demo/path", params=params, headers=headers).status_code == 200
    assert client.get("/api/demo/path", params=params, headers=headers).status_code == 429
    other = {"CF-Connecting-IP": "203.0.113.10"}
    assert client.get("/api/demo/path", params=params, headers=other).status_code == 200


def test_rejected_names_do_not_use_up_the_rate_limit(client, monkeypatch):
    monkeypatch.setitem(pf._DEMO_LIMITS, "path", (1, 3600))
    bad = {"src_name": "Meryl Streep", "tgt_name": "Nobody Private"}
    for _ in range(3):
        assert client.get("/api/demo/path", params=bad).status_code == 400
    ok = {"src_name": "Meryl Streep", "tgt_name": "Chuck Schumer"}
    assert client.get("/api/demo/path", params=ok).status_code == 200


def test_request_access_tags_demo_visitors(client):
    r = client.get("/request-access", params={"from": "demo"})
    assert "viaTag" in r.text
