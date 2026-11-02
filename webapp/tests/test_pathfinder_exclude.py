"""exclude: routing around go-betweens the user ruled out ("not this one?").

A path can run through the wrong person of a shared name -- "Michael Grunwald"
the Overbrook trustee continuing as a union officer of the same name. Excluding
a node must keep it out of every returned path, never drop an endpoint, and
report what was excluded. Runs on the real data in webapp/data/, igraph only.

Run with:
    python -m pytest webapp/tests/test_pathfinder_exclude.py -v
"""
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import pathfinder as pf  # noqa: E402

SRC, TGT = "Bill de Blasio", "Mark Peters"


@pytest.fixture(scope="module", autouse=True)
def _load_once():
    pf._load_igraph()
    pf._load_deceased()


def _nodes(p):
    return [s["node"] for s in p["path"]]


@pytest.fixture(scope="module")
def base():
    res = pf._find_path_igraph(SRC, TGT)
    assert res["paths"], res
    return res


def test_no_exclude_reports_empty(base):
    assert base["excluded"] == []


def test_excluded_go_between_is_avoided(base):
    first = _nodes(base["paths"][0])
    assert len(first) >= 3, first
    middle = first[1]
    res = pf._find_path_igraph(SRC, TGT, exclude=[middle])
    assert res["excluded"] == [middle]
    for p in res["paths"]:
        assert middle not in _nodes(p)


def test_endpoints_and_unknown_names_are_ignored(base):
    res = pf._find_path_igraph(SRC, TGT, exclude=[SRC, TGT, "No Such Node 12345"])
    assert res["excluded"] == []
    assert [_nodes(p) for p in res["paths"]] == [_nodes(p) for p in base["paths"]]


def test_exclude_is_capped():
    names = [f"No Such Node {i}" for i in range(50)]
    res = pf._find_path_igraph(SRC, TGT, exclude=names)
    assert res["excluded"] == []          # none exist; the cap just bounds the work


def test_shared_weight_arrays_untouched(base):
    before = pf._igraph_weight.copy(), pf._igraph_living_weight.copy()
    middle = _nodes(base["paths"][0])[1]
    pf._find_path_igraph(SRC, TGT, exclude=[middle])
    pf._find_path_igraph(SRC, TGT, exclude=[middle], include_deceased=True)
    assert (pf._igraph_weight == before[0]).all()
    assert (pf._igraph_living_weight == before[1]).all()
