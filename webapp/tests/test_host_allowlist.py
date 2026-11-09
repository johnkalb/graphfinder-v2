"""Only the real site names are served (2026-11-09): the DigitalOcean default
address bypassed Cloudflare Access and served the whole site."""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

pytestmark = pytest.mark.timeout(30)

DO_HOST = "graphapp-h75rm.ondigitalocean.app"


@pytest.mark.parametrize("path", ["/", "/debug", "/meminfo", "/request-access", "/api/search?q=smith"])
def test_unknown_host_gets_404(client, path):
    r = client.get(path, headers={"host": DO_HOST})
    assert r.status_code == 404 and r.text == "Not found"


def test_health_stays_open_on_any_host(client):
    assert client.get("/health", headers={"host": DO_HOST}).status_code != 404


@pytest.mark.parametrize("host", ["sixdegrees.net", "hooconnex.com", "www.hooconnex.com", "HooConnex.com:443"])
def test_real_site_names_are_served(client, host):
    assert client.get("/request-access", headers={"host": host}).status_code == 200


def test_extra_hosts_from_env(client, monkeypatch):
    monkeypatch.setenv("APP_ALLOWED_HOSTS", "staging.example.org")
    assert client.get("/request-access", headers={"host": "staging.example.org"}).status_code == 200
    assert client.get("/request-access", headers={"host": "sixdegrees.net"}).status_code == 404
