"""501(c)(3) charity-board stats and crawlies (2026-11-14)."""
import os
import sys

import igraph  # noqa: F401 -- build_graph_stats needs it
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_crawlie_facts as bcf  # noqa: E402
import build_graph_stats as bgs  # noqa: E402


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *a):
        pass

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.rows)


def test_charity_name():
    assert bcf.charity_name("THE NEW YORK AND PRESBYTERIAN HOSPITAL") == "The New York and Presbyterian Hospital"
    assert bcf.charity_name("RFE-RL INC") == "RFE-RL"
    assert bcf.charity_name("DISTRIBUTIVE EDUCATION CLUBS OF AMERICA GROUP RETURN") == \
        "Distributive Education Clubs of America"
    assert bcf.charity_name("Robin Hood Foundation") == "Robin Hood Foundation"
    assert bcf._distinct_charities(["THE NEW YORK AND PRESBYTERIAN HOSPITAL", "NEW YORK-PRESBYTERIAN FUND INC",
                                    "New York University", "Emory University", "EMORY"], 5) == \
        ["The New York and Presbyterian Hospital", "New York University", "Emory University"]


def test_nonprofit_stats_board_links_namesakes_and_communities():
    # two charity clusters (A1-A3, B1-B3) joined through one bridge person;
    # "Michael Smith" sits on every board but is too common a name to list
    orgs = ["Alpha Hospital", "Alpha Museum", "Alpha College", "Beta Hospital", "Beta Museum", "Beta College",
            "Acme Corp"]
    people = ["Ada Quill", "Bo Zent", "Cy Vorn", "Di Pask", "Ed Lunt", "Flo Graith", "Michael Smith"]
    fillers = [f"Michael Filler{i}" for i in range(40)] + [f"Pat Smith{i}" for i in range(2)] + \
              [f"Joe Smith" + "x" * i for i in range(40)]
    nodes = orgs + people + fillers
    ix = {n: i for i, n in enumerate(nodes)}
    mask = [n not in orgs for n in nodes]
    E = []

    def board(p, o, cat="CO_DIRECTOR"):
        E.append([ix[p], ix[o], 1.0, [cat]])
    for p in ("Ada Quill", "Bo Zent", "Cy Vorn"):
        for o in ("Alpha Hospital", "Alpha Museum", "Alpha College"):
            board(p, o)
    for p in ("Di Pask", "Ed Lunt", "Flo Graith"):
        for o in ("Beta Hospital", "Beta Museum", "Beta College"):
            board(p, o)
    board("Cy Vorn", "Beta Hospital")
    for o in orgs[:6]:
        board("Michael Smith", o)
    E.append([ix["Ada Quill"], ix["Acme Corp"], 1.0, ["CO_DIRECTOR"]])         # not a charity
    E.append([ix["Di Pask"], ix["Alpha College"], 1.0, ["EDUCATION"]])         # alumni, not a seat
    rows = [(o.upper(), "03", 5e6) for o in orgs[:6]] + [("ACME CORP", "05", 1e6)]
    verified = {n.lower() for n in people}
    s = bgs.nonprofit_stats(FakeConn(rows), nodes, E, mask, verified)
    assert s["c3_in_graph"] == 6 and s["board_people"] == 7
    assert s["multi_board_people"] == 7 and s["giant_charities"] == 6
    named = {m["name"] for m in s["most_boards"]}
    assert "Michael Smith" not in named and "Cy Vorn" in named
    assert s["most_boards"][0]["name"] == "Cy Vorn" and s["most_boards"][0]["boards"] == 4
    assert all(len(c["figures"]) == len(set(c["figures"])) for c in s["clusters"])
    assert not any("Michael Smith" in c["figures"] for c in s["clusters"])

    facts = bcf.nonprofit_facts(s, set())
    keys = [f["key"] for f in facts]
    assert keys[:2] == ["stats_nonprofits:count", "stats_nonprofits:web"]
    assert "stats_nonprofits:most_boards" in keys
    assert all(bcf.is_publishable(f, verified) for f in facts)
    # Wikidata unreachable: nothing that names a person
    assert all(not f["people"] for f in bcf.nonprofit_facts(s, None))
    # a dead figure is never named
    assert not any("Cy Vorn" in f["people"] for f in bcf.nonprofit_facts(s, {"cy vorn"}))


@pytest.mark.parametrize("stats", [None, {}, {"board_people": 0}])
def test_nonprofit_facts_empty(stats):
    assert bcf.nonprofit_facts(stats, set()) == []
