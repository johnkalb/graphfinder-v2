"""hooconnex landing page (/welcome) and the hooconnex-branded demo (2026-11-11)."""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)

HOO = {"host": "hooconnex.com"}


def test_welcome_on_hooconnex(client):
    r = client.get("/welcome", headers=HOO)
    assert r.status_code == 200
    body = r.text
    assert "__EDGES__" not in body and "__NODES__" not in body
    assert "million" in body
    assert 'href="/demo?ref=landing"' in body
    assert 'href="/request-access?from=landing"' in body
    assert 'href="/?signin=1"' in body          # sign-in link the redirect rule skips
    assert "sixdegrees" not in body.lower()


def test_welcome_not_on_sixdegrees(client):
    r = client.get("/welcome", headers={"host": "sixdegrees.net"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"


def test_demo_rebranded_on_hooconnex(client):
    body = client.get("/demo", headers=HOO).text
    assert "<title>hooconnex demo</title>" in body
    assert "hooconnex traces" in body and "full hooconnex graph" in body
    assert 'href="/welcome"' in body
    assert "sixdegrees" not in body.lower()


def test_demo_unchanged_on_sixdegrees(client):
    body = client.get("/demo", headers={"host": "sixdegrees.net"}).text
    assert "<title>sixdegrees.net demo</title>" in body
    assert 'href="/welcome"' not in body
