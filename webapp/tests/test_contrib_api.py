"""HTTP layer + graph routing for "Where have you been?" (contrib.py, Phase 1):
the beta gate, the affiliation lifecycle, routes that start from a place, and
the "N steps from" / "who from your world" payoff."""
import math

import numpy as np
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)
BETA = {"Cf-Access-Authenticated-User-Email": "alexkalb@gmail.com"}      # in _BETA_EMAILS
OTHER = {"Cf-Access-Authenticated-User-Email": "someone.else@example.com"}

# Acme is split into periods. Pat was at Acme 2016-2019 and is on the board of
# Big Charity with Famous Person. Old Timer was at Acme 1990-1993.
NAMES = ["Acme Corp", "Acme Corp (in 2016–2019)", "Acme Corp (in 1988–1991)", "Pat Jones", "Old Timer",
         "Big Charity", "Famous Person"]
EDGES = [(0, 1, 0.15), (0, 2, 0.15), (1, 3, 0.87), (2, 4, 0.87), (0, 3, 0.15), (0, 4, 0.15),
         (3, 5, 0.87), (5, 6, 0.87)]


@pytest.fixture(autouse=True)
def tiny_graph(monkeypatch):
    import igraph as ig
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "all")
    monkeypatch.delenv("CONTRIB_BETA", raising=False)
    g = ig.Graph(n=len(NAMES), edges=[(a, b) for a, b, _ in EDGES])
    probs = np.array([p for _, _, p in EDGES])
    w = -np.log(probs) + (-math.log(pf._FORWARD_PROB))
    for attr, val in {"_igraph_graph": g, "_igraph_nodes": NAMES,
                      "_igraph_name_to_idx": {n: i for i, n in enumerate(NAMES)},
                      "_igraph_weight": w, "_igraph_living_weight": w, "_igraph_prob": probs,
                      "_igraph_cats_mask": np.zeros(len(EDGES), dtype=np.uint32), "_igraph_cats_vocab": [],
                      "_igraph_deceased_idx": set()}.items():
        monkeypatch.setattr(pf, attr, val)
    monkeypatch.setattr(pf, "_load_igraph", lambda: None)
    monkeypatch.setattr(pf, "_load_deceased", lambda: {})
    monkeypatch.setattr(pf, "_get_label", lambda n: n)
    monkeypatch.setattr(pf, "_get_node_sci", lambda n: 50)
    monkeypatch.setattr(pf, "_resolve_name", lambda x: x if x in NAMES else None)
    monkeypatch.setattr(pf, "_famous_nodes", ["Famous Person"])
    pf._contrib_cache.clear()
    pf._invalidate_removed_links()
    yield


def _accept(client, headers):
    """Accept the agreement and start from no places (the test DB is shared)."""
    assert client.post("/api/agreement/accept", headers=headers).json()["success"]
    for a in client.get("/api/my/affiliations", headers=headers).json()["affiliations"]:
        client.delete(f"/api/my/affiliations/{a['id']}", headers=headers)


def test_beta_gate(client, monkeypatch):
    assert client.get("/api/my/affiliations").status_code == 401
    assert client.get("/api/my/affiliations", headers=OTHER).status_code == 404     # not a beta tester
    monkeypatch.setenv("CONTRIB_BETA", "all")
    assert client.get("/api/my/affiliations", headers=OTHER).status_code == 200


def test_lifecycle_and_privacy(client):
    r = client.post("/api/my/affiliations", json={"org": "Acme Corp", "start_year": 2017, "end_year": 2018},
                    headers=BETA)
    assert r.status_code == 428                     # agreement first
    _accept(client, BETA)
    assert client.post("/api/my/affiliations", json={"org": "Nowhere"}, headers=BETA).status_code == 400
    assert client.post("/api/my/affiliations", json={"org": "Pat Jones"}, headers=BETA).status_code == 400
    r = client.post("/api/my/affiliations", json={"org": "Acme Corp", "start_year": 2017, "end_year": 2018},
                    headers=BETA)
    assert r.json()["success"] and r.json()["affiliation"]["years"] == "2017–2018"
    aid = r.json()["affiliation"]["id"]
    assert [a["org"] for a in client.get("/api/my/affiliations", headers=BETA).json()["affiliations"]] == ["Acme Corp"]
    assert client.delete(f"/api/my/affiliations/{aid}", headers=BETA).json()["success"]
    assert client.get("/api/my/affiliations", headers=BETA).json()["affiliations"] == []


def test_same_years_route_beats_the_weak_org_link(client):
    _accept(client, BETA)
    client.post("/api/my/affiliations", json={"org": "Acme Corp", "start_year": 2017, "end_year": 2018}, headers=BETA)
    res = client.get("/api/my/reach", params={"target": "Famous Person"}, headers=BETA).json()
    best = res["paths"][0]
    assert [s["node"] for s in best["path"]][1:] == ["Acme Corp (in 2016–2019)", "Pat Jones", "Big Charity",
                                                      "Famous Person"]
    assert best["path"][0]["relation"] == "MY_AFFILIATION" and best["path"][0]["affiliation"]["same_years"]
    assert best["via_affiliation"] == "Acme Corp"
    assert len(res["paths"]) == 1                   # one route per place, not one per period


def test_start_payoff_counts_steps_and_people_from_your_years(client):
    _accept(client, BETA)
    s = client.get("/api/my/start", headers=BETA).json()
    assert s["n_places"] == 0 and s["degrees"][0]["steps"] is None
    client.post("/api/my/affiliations", json={"org": "Acme Corp", "start_year": 2017, "end_year": 2018}, headers=BETA)
    s = client.get("/api/my/start", headers=BETA).json()
    assert s["degrees"][0]["target"] == "Famous Person" and s["degrees"][0]["steps"] == 4
    w = s["world"][0]
    assert w["same_years"] and w["count"] == 1 and w["people"][0]["name"] == "Pat Jones"   # not Old Timer
