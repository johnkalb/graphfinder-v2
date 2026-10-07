"""src_via / tgt_via: picking WHICH person a shared name means.

"Mark Peters" in the real graph is one node holding several people (a NYC
Dept of Investigation commissioner, an AFGE union officer, an Iowa credit
union director, ...). Naming one connection must force every path to reach
the endpoint through it. Runs on the real data in webapp/data/, igraph only.

Run with:
    python -m pytest webapp/tests/test_pathfinder_via.py -v
"""
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import pathfinder as pf  # noqa: E402

NAMESAKE = "Mark Peters"
DOI_LINK = "New Yorkers for de Blasio"


@pytest.fixture(scope="module", autouse=True)
def _load_once():
    pf._load_igraph()
    pf._load_deceased()


def _nodes(p):
    return [s["node"] for s in p["path"]]


def test_src_via_forces_first_hop():
    res = pf._find_path_igraph(NAMESAKE, "Bill de Blasio", src_via=DOI_LINK)
    assert res["paths"], res
    for p in res["paths"]:
        nodes = _nodes(p)
        assert nodes[0] == NAMESAKE and nodes[1] == DOI_LINK
        assert nodes.count(NAMESAKE) == 1
        assert p["path"][0]["relation"]  # the added hop still gets its label
    assert res["src_via"] == DOI_LINK


def test_tgt_via_forces_last_hop():
    res = pf._find_path_igraph("Bill de Blasio", NAMESAKE, tgt_via=DOI_LINK)
    assert res["paths"], res
    for p in res["paths"]:
        assert _nodes(p)[-2:] == [DOI_LINK, NAMESAKE]


def test_via_is_the_other_endpoint():
    res = pf._find_path_igraph(NAMESAKE, "Bill de Blasio", src_via="Bill de Blasio")
    assert [_nodes(p) for p in res["paths"]] == [[NAMESAKE, "Bill de Blasio"]]


def test_via_must_be_a_connection():
    assert "error" in pf._find_path_igraph(NAMESAKE, "Bill de Blasio", src_via="Microsoft Corporation")
    assert "error" in pf._find_path_igraph(NAMESAKE, "Bill de Blasio", src_via="No Such Node 123")
