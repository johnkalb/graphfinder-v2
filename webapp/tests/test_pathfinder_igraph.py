"""Equivalence check: igraph-backed _find_path_igraph() vs the in-memory
NetworkX _find_path(), over the real graph data in webapp/data/.

Unlike test_pathfinder_pg.py, this has no external dependency (no database,
no DATABASE_URL) -- both backends load straight from the static
graph_scored*.json.gz files already in webapp/data/, so this runs as part
of the normal test suite.

Run with:
    python -m pytest webapp/tests/test_pathfinder_igraph.py -v
"""
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

import pathfinder as pf  # noqa: E402

# Exact canonical node strings (verified against graph_scored.json.gz) --
# _find_path_igraph() is alias-aware via the same _resolve_name() _find_path()
# uses, so exact-match pairs aren't strictly required here (unlike the
# pgRouting variant's resolve()), but keeping them exact isolates the
# comparison to the search-backend difference rather than name resolution.
KNOWN_PAIRS = [
    ("Donald Trump", "Gavin Newsom"),
    ("Donald Trump", "Elon Musk"),
    ("Elon Musk", "Nancy Pelosi"),
]

# A deceased person known to be present in webapp/data/deceased.json, used
# to exercise _find_path_igraph()'s weight-masking logic for the "deceased
# person searched as an endpoint" case -- the one part of the igraph design
# with no direct NetworkX-side line-by-line equivalent to diff against (see
# _find_path_igraph()'s docstring).
DECEASED_PAIR = ("Alan Greenspan", "Donald Trump")


@pytest.fixture(scope="module", autouse=True)
def _load_graphs_once():
    pf._load_graph()
    pf._load_igraph()
    pf._load_deceased()


def _paths_summary(result):
    """(node sequence, rounded probability) tuples, order-preserving."""
    assert "error" not in result, result
    return [
        (tuple(step["node"] for step in p["path"]), round(p["probability"], 4))
        for p in result["paths"]
    ]


def _assert_equivalent(nx_paths, ig_paths, label):
    """Both backends compute path probability with the identical formula
    (_path_probability, shared by _find_path/_find_path_igraph), so a graph
    with several equal-cost intermediaries legitimately produces *multiple*
    correct "shortest" paths of identical probability (see
    test_pathfinder_pg.py's _assert_equivalent for the concrete Trump/Newsom
    example). NetworkX's Yen's algorithm and igraph's get_k_shortest_paths
    break those ties differently, so which member of a tied group comes
    back isn't required to match -- only that both backends agree on the
    multiset of rank probabilities, and that any rank whose probability is
    *unique* (no tie) also has a matching node sequence."""
    import collections
    nx_probs = collections.Counter(p for _, p in nx_paths)
    ig_probs = collections.Counter(p for _, p in ig_paths)
    assert ig_probs == nx_probs, (
        f"{label}: probability multiset differs\nnetworkx: {nx_paths}\nigraph: {ig_paths}"
    )
    nx_by_prob = collections.defaultdict(set)
    for nodes, p in nx_paths:
        nx_by_prob[p].add(nodes)
    ig_by_prob = collections.defaultdict(set)
    for nodes, p in ig_paths:
        ig_by_prob[p].add(nodes)
    for p, count in nx_probs.items():
        if count == 1:  # unique (untied) rank -- exact node sequence must match
            assert ig_by_prob[p] == nx_by_prob[p], (
                f"{label}: untied rank at probability {p} differs\n"
                f"networkx: {nx_by_prob[p]}\nigraph: {ig_by_prob[p]}"
            )


@pytest.mark.parametrize("src,tgt", KNOWN_PAIRS)
def test_igraph_matches_networkx(src, tgt):
    nx_result = pf._find_path(src, tgt, k=3)
    ig_result = pf._find_path_igraph(src, tgt, k=3)

    nx_paths = _paths_summary(nx_result)
    ig_paths = _paths_summary(ig_result)

    assert nx_paths, f"NetworkX backend found no paths {src!r} -> {tgt!r}, nothing to compare"
    _assert_equivalent(nx_paths, ig_paths, f"{src!r} -> {tgt!r}")


@pytest.mark.parametrize("src,tgt", KNOWN_PAIRS)
def test_igraph_matches_networkx_include_deceased(src, tgt):
    nx_result = pf._find_path(src, tgt, k=3, include_deceased=True)
    ig_result = pf._find_path_igraph(src, tgt, k=3, include_deceased=True)

    nx_paths = _paths_summary(nx_result)
    ig_paths = _paths_summary(ig_result)

    _assert_equivalent(nx_paths, ig_paths, f"{src!r} -> {tgt!r} (include_deceased)")


def test_igraph_matches_networkx_deceased_endpoint():
    """Regression case for the weight-masking design: a deceased person
    searched as an endpoint (allowed) must still be reachable while other
    deceased people stay excluded as intermediaries (include_deceased=False,
    the default)."""
    src, tgt = DECEASED_PAIR
    assert src.lower() in pf._load_deceased(), f"{src!r} must be in deceased.json for this test to be meaningful"

    nx_result = pf._find_path(src, tgt, k=3, include_deceased=False)
    ig_result = pf._find_path_igraph(src, tgt, k=3, include_deceased=False)

    nx_paths = _paths_summary(nx_result)
    ig_paths = _paths_summary(ig_result)

    assert nx_paths, f"NetworkX backend found no paths {src!r} -> {tgt!r}, nothing to compare"
    _assert_equivalent(nx_paths, ig_paths, f"{src!r} -> {tgt!r} (deceased endpoint)")
    assert nx_result["deceased_excluded"] == ig_result["deceased_excluded"]
