"""Equivalence check: pgRouting-backed _find_path_pg() vs the in-memory
NetworkX _find_path(), over the real graph data in webapp/data/.

Requires a Postgres+pgRouting DATABASE_URL pointed at a database already
loaded via webapp/load_graph_to_postgres.py (see webapp/migrations/001).
Skipped entirely when DATABASE_URL isn't set -- this is not part of the
default (sqlite-only) test suite.

Run with:
    DATABASE_URL=postgresql://... python -m pytest webapp/tests/test_pathfinder_pg.py -v
"""
import os
import sys
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires DATABASE_URL (Postgres+pgRouting)"
)

import pathfinder as pf  # noqa: E402

# Pairs whose names are exact canonical node strings (verified against
# graph_scored.json.gz). _find_path_pg's resolve() now canonicalizes
# through the same alias-aware _resolve_name() _find_path() uses before
# querying Postgres, so alias/alternate-spelling inputs are no longer a
# known gap -- these are kept as exact matches anyway to isolate the
# comparison to the search-backend difference (NetworkX vs pgr_ksp).
KNOWN_PAIRS = [
    ("Donald Trump", "Gavin Newsom"),
    ("Donald Trump", "Elon Musk"),
    ("Elon Musk", "Nancy Pelosi"),
]


@pytest.fixture(scope="module", autouse=True)
def _load_graph_once():
    pf._load_search()  # populates _canonical_map, used by _resolve_name()
    pf._load_graph()
    pf._load_deceased()


def _paths_summary(result):
    """(node sequence, rounded probability) tuples, order-preserving."""
    assert "error" not in result, result
    return [
        (tuple(step["node"] for step in p["path"]), round(p["probability"], 4))
        for p in result["paths"]
    ]


def _assert_equivalent(nx_paths, pg_paths, label):
    """Both backends compute path probability with the identical formula
    (_path_probability, shared by _find_path/_find_path_pg), so a graph with
    several equal-cost intermediaries (e.g. two people with the same
    contributing-category probability on both hops) legitimately produces
    *multiple* correct "shortest" paths of identical probability -- verified
    directly against the DB for the Trump/Newsom pair (Tom Daschle and John
    Kerry both hit 0.8947*0.8947 exactly). NetworkX's Yen's algorithm and
    pgRouting's KSP break those ties differently, so which member of a tied
    group comes back isn't required to match -- only that both backends
    agree on the multiset of rank probabilities, and that any rank whose
    probability is *unique* (no tie) also has a matching node sequence."""
    import collections
    nx_probs = collections.Counter(p for _, p in nx_paths)
    pg_probs = collections.Counter(p for _, p in pg_paths)
    assert pg_probs == nx_probs, (
        f"{label}: probability multiset differs\nnetworkx: {nx_paths}\npgrouting: {pg_paths}"
    )
    nx_by_prob = collections.defaultdict(set)
    for nodes, p in nx_paths:
        nx_by_prob[p].add(nodes)
    pg_by_prob = collections.defaultdict(set)
    for nodes, p in pg_paths:
        pg_by_prob[p].add(nodes)
    for p, count in nx_probs.items():
        if count == 1:  # unique (untied) rank -- exact node sequence must match
            assert pg_by_prob[p] == nx_by_prob[p], (
                f"{label}: untied rank at probability {p} differs\n"
                f"networkx: {nx_by_prob[p]}\npgrouting: {pg_by_prob[p]}"
            )


@pytest.mark.parametrize("src,tgt", KNOWN_PAIRS)
def test_pg_matches_networkx(src, tgt):
    nx_result = pf._find_path(src, tgt, k=3)
    pg_result = pf._find_path_pg(src, tgt, k=3)

    nx_paths = _paths_summary(nx_result)
    pg_paths = _paths_summary(pg_result)

    assert nx_paths, f"NetworkX backend found no paths {src!r} -> {tgt!r}, nothing to compare"
    _assert_equivalent(nx_paths, pg_paths, f"{src!r} -> {tgt!r}")


@pytest.mark.parametrize("src,tgt", KNOWN_PAIRS)
def test_pg_matches_networkx_include_deceased(src, tgt):
    nx_result = pf._find_path(src, tgt, k=3, include_deceased=True)
    pg_result = pf._find_path_pg(src, tgt, k=3, include_deceased=True)

    nx_paths = _paths_summary(nx_result)
    pg_paths = _paths_summary(pg_result)

    _assert_equivalent(nx_paths, pg_paths, f"{src!r} -> {tgt!r} (include_deceased)")
