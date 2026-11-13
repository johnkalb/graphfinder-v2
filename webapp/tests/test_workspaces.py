"""Team workspaces (hooconnex Teams Phase 1, 2026-11-13): contact parsing,
matching, membership rules and -- above all -- that one workspace's data is
never visible to anyone outside it, on any endpoint."""
import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401 -- fixtures are used by name

import workspaces as ws

pytestmark = pytest.mark.timeout(30)

SITE_ADMIN = {"Cf-Access-Authenticated-User-Email": "kureious09@gmail.com"}
ALICE = {"Cf-Access-Authenticated-User-Email": "alice@acme.example"}      # admin of Acme
BOB = {"Cf-Access-Authenticated-User-Email": "bob@acme.example"}          # member of Acme
EVE = {"Cf-Access-Authenticated-User-Email": "eve@rival.example"}         # admin of Rival

NODES = {"barack obama": "Barack Obama", "jane doe": "Jane Doe", "mark greene": "Mark Greene",
         "mark greene (ibm)": "Mark Greene (Ibm)", "mark greene (acme corp)": "Mark Greene (Acme Corp)"}

LINKEDIN = (b"Notes:\n\"When exporting your connection data, you may notice...\"\n\n"
            b"First Name,Last Name,URL,Email Address,Company,Position,Connected On\n"
            b"Jane,Doe,https://x,jane@x.com,Foo Inc,CEO,01 Jan 2024\n"
            b"Mark,Greene,https://y,,IBM,Engineer,02 Jan 2024\n"
            b"Mark,Greene,https://z,,Unknown LLC,Analyst,03 Jan 2024\n"
            b"Zed,Nobody,https://w,,Nowhere,Intern,04 Jan 2024\n")


def test_parse_linkedin_google_and_vcard():
    rows = ws.parse_contacts(LINKEDIN, "Connections.csv")
    assert rows[0] == {"name": "Jane Doe", "company": "Foo Inc", "title": "CEO"}
    assert len(rows) == 4 and all("email" not in r for r in rows)
    google = b"Name,Given Name,Family Name,Organization 1 - Name,Organization 1 - Title\nAl Pha,Al,Pha,Beta Co,VP\n"
    assert ws.parse_contacts(google, "contacts.csv") == [{"name": "Al Pha", "company": "Beta Co", "title": "VP"}]
    vcf = b"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Cee Dee\r\nORG:Gamma Ltd;Sales\r\nTITLE:Rep\r\nTEL:555\r\nEND:VCARD\r\n"
    assert ws.parse_contacts(vcf, "a.vcf") == [{"name": "Cee Dee", "company": "Gamma Ltd", "title": "Rep"}]
    assert ws.parse_contacts(b"First Name,Last Name\nCher,\n", "x.csv") == []    # single names are dropped


@pytest.fixture
def setup(client, monkeypatch):
    monkeypatch.setenv("WORKSPACES_BETA", "all")
    monkeypatch.setattr(pf, "_load_node_lookup", lambda: NODES)
    monkeypatch.setattr(pf, "_namesake_index", None)
    monkeypatch.setattr(pf, "_ws_allow_through_access", lambda email: (True, "ok"))
    for h in (SITE_ADMIN, ALICE, BOB, EVE):
        assert client.post("/api/agreement/accept", headers=h).status_code == 200
    acme = client.post("/api/ws", json={"name": "Acme", "admin_email": "alice@acme.example", "seat_limit": 2},
                       headers=SITE_ADMIN).json()["id"]
    rival = client.post("/api/ws", json={"name": "Rival", "admin_email": "eve@rival.example"},
                        headers=SITE_ADMIN).json()["id"]
    assert client.post(f"/api/ws/{acme}/members", json={"email": "bob@acme.example"}, headers=ALICE).json()["success"]
    r = client.post(f"/api/ws/{acme}/contacts", files={"file": ("Connections.csv", LINKEDIN, "text/csv")}, headers=BOB)
    assert r.status_code == 200, r.text
    return acme, rival, r.json()


def test_only_site_admins_create_and_beta_flag(client, setup, monkeypatch):
    r = client.post("/api/ws", json={"name": "X", "admin_email": "a@b.co"}, headers=ALICE)
    assert r.status_code == 403
    monkeypatch.setenv("WORKSPACES_BETA", "admins")
    assert client.get("/api/ws", headers=ALICE).status_code == 404
    assert client.get("/api/ws").status_code == 401


def test_upload_matching(client, setup):
    acme, _, res = setup
    # Jane: exact, no namesakes. Mark (IBM): company picks the split. Mark (Unknown LLC): several
    # possibilities -> a pick. Zed: not in the network.
    assert (res["matched"], res["ambiguous"], res["unmatched"]) == (2, 1, 1)
    rows = {(c["name"], c["company"]): c for c in client.get(f"/api/ws/{acme}/contacts", headers=ALICE).json()["contacts"]}
    assert rows[("Mark Greene", "IBM")]["node"] == "Mark Greene (Ibm)"
    amb = rows[("Mark Greene", "Unknown LLC")]
    assert amb["status"] == "ambiguous" and set(amb["candidates"]) == {"Mark Greene", "Mark Greene (Ibm)",
                                                                       "Mark Greene (Acme Corp)"}
    # settle it; a name that isn't a node is refused
    assert client.post(f"/api/ws/{acme}/contacts/{amb['id']}/resolve", json={"node": "Not A Node"},
                       headers=ALICE).status_code == 400
    assert client.post(f"/api/ws/{acme}/contacts/{amb['id']}/resolve", json={"node": "Mark Greene (Acme Corp)"},
                       headers=ALICE).json()["success"]
    assert client.get(f"/api/ws/{acme}", headers=BOB).json()["contacts"] == {"matched": 3, "unmatched": 1}


def test_reupload_replaces_and_source_delete(client, setup):
    acme, _, _ = setup
    again = b"First Name,Last Name,Company\nJane,Doe,Foo\n"
    r = client.post(f"/api/ws/{acme}/contacts", files={"file": ("Connections.csv", again, "text/csv")}, headers=BOB).json()
    assert r["replaced_previous"] and r["rows"] == 1
    assert [c["name"] for c in client.get(f"/api/ws/{acme}/contacts", headers=BOB).json()["contacts"]] == ["Jane Doe"]
    src = client.get(f"/api/ws/{acme}/sources", headers=BOB).json()["sources"]
    assert len(src) == 1
    assert client.delete(f"/api/ws/{acme}/sources/{src[0]['id']}", headers=BOB).json()["contacts_removed"] == 1
    assert client.get(f"/api/ws/{acme}/contacts", headers=BOB).json()["contacts"] == []


def test_member_rules(client, setup):
    acme, _, _ = setup
    # seat limit 2 is full (alice + bob)
    r = client.post(f"/api/ws/{acme}/members", json={"email": "carl@acme.example"}, headers=ALICE)
    assert r.status_code == 409
    # members can't manage members or read the audit log
    assert client.post(f"/api/ws/{acme}/members", json={"email": "c@d.co"}, headers=BOB).status_code == 403
    assert client.get(f"/api/ws/{acme}/audit", headers=BOB).status_code == 403
    # the last admin can't be removed
    assert client.delete(f"/api/ws/{acme}/members/alice@acme.example", headers=ALICE).status_code == 409
    assert client.delete(f"/api/ws/{acme}/members/bob@acme.example", headers=ALICE).json()["success"]
    assert client.get(f"/api/ws/{acme}", headers=BOB).status_code == 404
    actions = [e["action"] for e in client.get(f"/api/ws/{acme}/audit", headers=ALICE).json()["events"]]
    assert {"member_added", "contacts_uploaded", "member_removed"} <= set(actions)


def test_isolation_every_endpoint(client, setup):
    """Eve (another workspace's admin) and the site admin (not a member) see
    nothing of Acme's on any endpoint, and get the same 404 as for a
    workspace that doesn't exist."""
    acme, rival, _ = setup
    eid = client.get(f"/api/ws/{acme}/contacts", headers=ALICE).json()["contacts"][0]["id"]
    sid = client.get(f"/api/ws/{acme}/sources", headers=ALICE).json()["sources"][0]["id"]
    probes = [("get", f"/api/ws/{acme}", None), ("get", f"/api/ws/{acme}/members", None),
              ("get", f"/api/ws/{acme}/contacts", None), ("get", f"/api/ws/{acme}/sources", None),
              ("get", f"/api/ws/{acme}/export", None), ("get", f"/api/ws/{acme}/audit", None),
              ("get", f"/api/ws/{acme}/reach?target=Barack%20Obama", None),
              ("post", f"/api/ws/{acme}/members", {"email": "eve2@rival.example"}),
              ("post", f"/api/ws/{acme}/contacts/{eid}/resolve", {"node": None}),
              ("delete", f"/api/ws/{acme}/contacts/{eid}", None), ("delete", f"/api/ws/{acme}/sources/{sid}", None),
              ("delete", f"/api/ws/{acme}/members/bob@acme.example", None), ("delete", f"/api/ws/{acme}", None)]
    for who in (EVE, SITE_ADMIN):
        for method, url, body in probes:
            r = getattr(client, method)(url, headers=who, **({"json": body} if body is not None else {}))
            assert r.status_code == 404, (who, method, url, r.status_code)
            assert "Acme" not in r.text and "Jane" not in r.text
        r = client.post(f"/api/ws/{acme}/contacts", files={"file": ("x.csv", b"First Name,Last Name\nA B,C\n")},
                        headers=who)
        assert r.status_code == 404
    # a workspace id that doesn't exist looks exactly the same
    assert client.get("/api/ws/99999", headers=EVE).json() == client.get(f"/api/ws/{acme}", headers=EVE).json()
    # Eve's own listing shows only Rival; nothing was changed by her probes
    # (the test client's DB is shared by the whole session, so earlier tests' Rivals are listed too)
    assert {w["name"] for w in client.get("/api/ws", headers=EVE).json()["workspaces"]} == {"Rival"}
    assert len(client.get(f"/api/ws/{acme}/contacts", headers=ALICE).json()["contacts"]) == 4
    assert client.get(f"/api/ws/{rival}/contacts", headers=EVE).json()["contacts"] == []


def test_reach_uses_only_this_workspace_and_names_the_colleague(client, setup, monkeypatch):
    acme, rival, _ = setup
    client.post(f"/api/ws/{rival}/contacts", files={"file": ("r.csv", b"First Name,Last Name\nBarack,Obama\n")},
                headers=EVE)
    seen = {}

    def fake_reach(owner, contacts, target, k):
        seen["contacts"] = {c["contact"] for c in contacts}
        return {"paths": [{"via_contact": "Jane Doe", "length": 2, "band": "Plausible",
                           "path": [{"node": owner, "label": owner, "relation": "MY_LINK"},
                                    {"node": "Jane Doe", "label": "Jane Doe"}, {"node": target, "label": target}]}]}
    monkeypatch.setattr(pf, "_reach_via_contacts", fake_reach)
    monkeypatch.setattr(pf, "_display_name_for_email", lambda e: {"bob@acme.example": "Bob Builder"}.get(e))
    r = client.get(f"/api/ws/{acme}/reach", params={"target": "Barack Obama"}, headers=ALICE).json()
    assert seen["contacts"] == {"Jane Doe", "Mark Greene (Ibm)"}          # not Rival's Obama, not unmatched ones
    assert r["paths"][0]["path"][0]["label"] == "Bob Builder" and r["contacts_considered"] == 2


def test_delete_workspace_erases_contacts(client, setup):
    acme, _, _ = setup
    assert client.delete(f"/api/ws/{acme}", headers=BOB).status_code == 403
    assert client.delete(f"/api/ws/{acme}", headers=ALICE).json()["success"]
    assert client.get(f"/api/ws/{acme}", headers=ALICE).status_code == 404
    conn = ws.db.connect(pf._ml_db())
    assert conn.execute("SELECT count(*) AS n FROM ws_entities WHERE workspace_id = ?", (acme,)).fetchone()["n"] == 0
    conn.close()
