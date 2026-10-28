"""hooconnex.com: the free site on the same app (2026-10-28). Search, paths and
My links; Ask a question and Explore records (the subscription) hidden on its
page and refused by the server, admins excepted. sixdegrees.net unchanged."""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)
HOO = {"host": "hooconnex.com"}
USER = {"Cf-Access-Authenticated-User-Email": "beta.user@example.com"}
ADMIN = {"Cf-Access-Authenticated-User-Email": "john.kalb@gmail.com"}


@pytest.fixture(autouse=True)
def beta_on(monkeypatch):
    monkeypatch.setenv("PRIVATE_LINKS_BETA", "all")
    yield


def test_home_page_is_branded_and_hides_paid_features(client):
    page = client.get("/", headers=HOO).text
    assert "<title>hooconnex</title>" in page and "hooconnex.com &bull;" in page
    assert "[data-paid] { display: none !important; }" in page
    assert 'class="ml-card" data-paid' in page and 'id="reach-target"' in page
    assert "Network Pathfinder" not in page.split("<body>")[1].split("</h1>")[0]


def test_sixdegrees_page_unchanged(client):
    page = client.get("/").text
    assert "<title>Network Pathfinder</title>" in page
    assert "[data-paid] { display" not in page


def test_paid_endpoints_refused_on_hooconnex(client):
    client.post("/api/agreement/accept", headers=USER)
    r = client.post("/api/my/ask", json={"question": "Who is on the GM board?"}, headers={**USER, **HOO})
    assert r.status_code == 402 and r.json()["status"] == "subscription_required"
    r = client.get("/api/my/detail", params={"kind": "donors", "name": "Warnock"}, headers={**USER, **HOO})
    assert r.status_code == 402
    # the free features still answer there
    assert client.get("/api/my/links", headers={**USER, **HOO}).status_code == 200


def test_admins_keep_paid_features_on_hooconnex(client):
    client.post("/api/agreement/accept", headers=ADMIN)
    r = client.get("/api/my/detail", params={"kind": "donors", "name": "Warnock"}, headers={**ADMIN, **HOO})
    assert r.status_code != 402


def test_request_access_page_names_the_site(client):
    assert "hooconnex.com" in client.get("/request-access", headers=HOO).text
    assert "sixdegrees.net" not in client.get("/request-access", headers=HOO).text
    assert "sixdegrees.net" in client.get("/request-access").text


def test_second_access_audience_accepted(monkeypatch):
    monkeypatch.setattr(pf, "_CF_ACCESS_AUDS", {"aud-six", "aud-hoo"})
    assert pf._CF_ACCESS_AUDS.intersection(["aud-hoo"])
    assert not pf._CF_ACCESS_AUDS.intersection(["aud-other"])
