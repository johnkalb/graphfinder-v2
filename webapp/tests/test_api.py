"""Tests for the 7 user-facing API endpoints of the sixdegrees.net pathfinder webapp.

Run with:
    cd /mnt/c/Users/johnk/graphfinder-clean && python -m pytest webapp/tests/ -v --timeout=30

Notes on how this suite was built (from directly profiling pathfinder.py
against the real data in webapp/data/ before writing any assertions):

- The app loads only the search index at FastAPI startup; the 240K-node
  graph loads lazily on the first call that needs it (see `_load_graph`'s
  docstring and `startup()`). A cold `/api/path` call pays that load cost
  (~11s here) on top of the actual pathfind. The session-scoped `client`
  fixture below issues one throwaway `/api/path` request to absorb both
  costs, so the *timed* tests measure steady-state latency, not cold start.

- `/api/path` calls `_generate_path_narrative()` for the top path, which --
  because a real GOOGLE_API_KEY is discoverable in this environment's
  `.env` files -- makes a live network call to the Gemini API. That would
  make this suite non-hermetic (network flakiness, real API cost, and
  latency that has nothing to do with the code under test), so
  `_generate_path_narrative` is monkeypatched to a no-op for the whole
  session.

- Even fully warm, `shortest_simple_paths` (Yen's algorithm) over this
  240K-node graph takes ~6-10s per query (profiled directly, no HTTP/ASGI
  overhead) -- see test_path_response_under_5s below. That is measured,
  reproducible fact, not a fixture artifact, and it does not meet the 5s
  target in the requirements. The assertion is left as specified so the
  suite reports the real number rather than hiding it.

- A few endpoint behaviors differ from the original spec this suite was
  written against; each is called out with "SPEC MISMATCH" at the
  relevant test, with the actual (verified) behavior asserted instead of
  the assumed one.
"""
import gzip
import json
import sys
import time
from pathlib import Path

import pytest

WEBAPP_DIR = Path(__file__).resolve().parents[1]
if str(WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(WEBAPP_DIR))

from fastapi.testclient import TestClient  # noqa: E402
import pathfinder as pf  # noqa: E402

# The session-scoped `client` fixture below pays for graph load (~11s here)
# plus one full k=5 shortest_simple_paths query (~7-10s, see the profiling
# notes above) during whichever test triggers it first. Override the
# per-test default here so that one-time cost doesn't trip the CLI's
# `--timeout=30` on an otherwise-unrelated test. The response-time
# assertions inside each test are the real SLA check; this is just wall
# clock headroom for the fixture.
#
# Bumped 60 -> 150: the underlying graph/search-index have grown
# substantially since this budget was set (search index alone is now
# 1.79M entries; the graph has grown from ~2M to 14M+ edges via the daily
# "Refresh scored graph" pipeline), so the one-time cold graph load + first
# Yen's-algorithm path query now regularly exceeds 60s on its own -- a
# pre-existing, data-growth-driven slowdown unrelated to any of the
# assertions this file actually checks. Real path-finding performance is a
# separate, larger problem (candidate for the entity-type-system /
# indexing follow-up), not something to paper over by inflating this
# number indefinitely if it keeps growing.
pytestmark = pytest.mark.timeout(150)


# --------------------------------------------------------------------------
# Session-scoped fixtures: one process-wide app + one warm graph load.
# --------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _silence_path_narrative():
    """Prevent /api/path from making a real Gemini API call during tests."""
    mp = pytest.MonkeyPatch()
    mp.setattr(pf, "_generate_path_narrative", lambda path_obj: None)
    yield
    mp.undo()


@pytest.fixture(scope="session")
def client():
    with TestClient(pf.app) as c:
        # Absorb the lazy graph load + first-query cost here, outside of
        # any timed assertion.
        c.get("/api/path", params={"src_name": "Donald Trump", "tgt_name": "Gavin Newsom"})
        yield c


# ==========================================================================
# 1. GET /api/search
# ==========================================================================

def test_search_known_query_returns_results(client):
    r = client.get("/api/search", params={"q": "trump"})
    assert r.status_code == 200
    results = r.json()
    assert isinstance(results, list)
    assert len(results) > 0

    # SPEC MISMATCH: entries carry {canonical, normalized, degree, aliases},
    # not {id, name, label, type, degree} -- verified against the live
    # search_index.json / _find_entry() return shape, not assumed.
    first = results[0]
    for field in ("canonical", "normalized", "degree", "aliases"):
        assert field in first
    assert any(e["canonical"] == "Donald Trump" for e in results)


def test_search_excludes_unresolved_id_placeholder_entities(client):
    # Found live on the deployed site: ingestion pipelines whose source
    # scripts aren't in this repo left literal fallback strings as node
    # names when real-name resolution failed -- e.g. "FEC Campaign
    # Committee C0000093" (the raw FEC committee ID substituted in), and
    # an even more degraded "FEC Campaign Committee " (trailing space, no
    # ID at all) bucketing every unresolvable committee into one node.
    # Some of these have enormous degree (C0000093 = 7136; the no-ID
    # bucket = 39606), so the degree-based score boost in _find_entry was
    # pushing known-junk placeholder names to the top of real search
    # results. Verified directly against the live search_index.json
    # before writing this assertion (see pathfinder._is_placeholder_entity).
    r = client.get("/api/search", params={"q": "committee"})
    assert r.status_code == 200
    results = r.json()
    assert len(results) > 0
    for entry in results:
        assert not pf._is_placeholder_entity(entry["canonical"]), entry["canonical"]
    # The real, legitimately-named committees must still be found --
    # this isn't just "committee" returning nothing.
    assert any("National Committee" in e["canonical"] for e in results)


def test_search_nonexistent_query_returns_empty_list(client):
    r = client.get("/api/search", params={"q": "xyzzy_nonexistent_12345"})
    assert r.status_code == 200
    assert r.json() == []


def test_search_single_char_query_returns_empty_list(client):
    r = client.get("/api/search", params={"q": "x"})
    assert r.status_code == 200
    assert r.json() == []


def test_search_response_time_under_2s(client):
    t0 = time.perf_counter()
    r = client.get("/api/search", params={"q": "trump"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, f"/api/search took {elapsed:.3f}s (target < 2s)"


# ==========================================================================
# 2. GET /api/path
# ==========================================================================

def test_path_missing_params_returns_error(client):
    r = client.get("/api/path")
    assert r.status_code == 200
    assert r.json() == {"error": "Both src_name and tgt_name required"}


def test_path_unknown_src_returns_error(client):
    """SPEC MISMATCH: an unresolvable src/tgt name short-circuits inside
    _find_path() (see _resolve_name()) and returns a bare {"error": ...}
    with NO "src_found" key at all -- "src_found": false is only ever
    produced further downstream (NetworkXNoPath / NodeNotFound branches),
    which an unresolvable name never reaches. Verified directly against
    _find_path() before writing this assertion.
    """
    r = client.get(
        "/api/path",
        params={"src_name": "Zzznonexistentperson999xyz", "tgt_name": "Gavin Newsom"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "error" in body
    assert "Zzznonexistentperson999xyz" in body["error"]
    assert "src_found" not in body


def test_path_trump_to_newsom_finds_a_path(client):
    r = client.get(
        "/api/path",
        params={"src_name": "Donald Trump", "tgt_name": "Gavin Newsom"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("src_found") is True
    assert body.get("tgt_found") is True
    assert len(body["paths"]) >= 1

    path0 = body["paths"][0]
    for field in ("length", "probability", "link_prob", "forward_prob", "band", "path", "guided_probability"):
        assert field in path0, f"missing field {field!r} on path object"
    assert isinstance(path0["path"], list) and len(path0["path"]) >= 2

    for step in path0["path"]:
        for field in ("node", "label", "relation", "prob", "cats"):
            assert field in step, f"missing field {field!r} on path step"


def test_path_include_deceased_param_accepted(client):
    r = client.get(
        "/api/path",
        params={"src_name": "Donald Trump", "tgt_name": "Gavin Newsom", "include_deceased": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("include_deceased") is True
    assert len(body["paths"]) >= 1


def test_path_response_under_5s(client):
    """Measured, reproducible fact (profiled independently of this test
    file, with the graph already warm): shortest_simple_paths over this
    240K-node graph costs roughly 6-10s per query, so this assertion is
    expected to fail against the 5s target in the current codebase. Left
    as specified rather than loosened, so the suite reports the real gap.
    """
    t0 = time.perf_counter()
    r = client.get(
        "/api/path",
        params={"src_name": "Donald Trump", "tgt_name": "Gavin Newsom"},
    )
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 5.0, f"/api/path took {elapsed:.3f}s (target < 5s)"


# ==========================================================================
# 3. GET /api/names
# ==========================================================================

def test_names_returns_gzip_json_with_known_key(client):
    r = client.get("/api/names")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"

    decompressed = gzip.decompress(r.content)
    data = json.loads(decompressed)
    assert isinstance(data, dict)
    assert "donald trump" in data


def test_names_response_time_under_1s(client):
    t0 = time.perf_counter()
    r = client.get("/api/names")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 1.0, f"/api/names took {elapsed:.3f}s (target < 1s)"


# ==========================================================================
# 4. GET /api/evidence
# ==========================================================================

# A real (src, tgt, rel) edge confirmed present in webapp/data/evidence.json.gz
KNOWN_EVIDENCE = {"src": "michael davis", "tgt": "wachovia bk na fl", "rel": "COMMUNICATED_WITH"}


def test_evidence_known_edge_returns_array_with_expected_fields(client):
    r = client.get("/api/evidence", params=KNOWN_EVIDENCE)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["evidence"], list)
    assert len(body["evidence"]) > 0
    for item in body["evidence"]:
        for field in ("source", "snippet", "doc", "page"):
            assert field in item


def test_evidence_unknown_edge_returns_empty_list(client):
    r = client.get(
        "/api/evidence",
        params={"src": "nobody_xyz", "tgt": "nowhere_xyz", "rel": "MADE_UP_RELATION_TYPE"},
    )
    assert r.status_code == 200
    assert r.json() == {"evidence": []}


def test_evidence_known_relation_type_falls_back_to_source_attribution(client):
    """No per-edge snippet exists for this made-up pair, but FELLOW_JUDGE is
    a known bulk-source relation type (_REL_SOURCE), so the endpoint should
    fall back to a source-attribution stub rather than an empty list.
    """
    r = client.get(
        "/api/evidence",
        params={"src": "nobody_xyz", "tgt": "nowhere_xyz", "rel": "FELLOW_JUDGE"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["evidence"]) == 1
    assert body["evidence"][0]["source"] == "US Courts (same circuit)"


def test_evidence_response_time_under_2s(client):
    t0 = time.perf_counter()
    r = client.get("/api/evidence", params=KNOWN_EVIDENCE)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 2.0, f"/api/evidence took {elapsed:.3f}s (target < 2s)"


# ==========================================================================
# 5. GET /api/relation-info
# ==========================================================================

def test_relation_info_fellow_representative(client):
    """SPEC MISMATCH: 'FELLOW_REPRESENTATIVE' is only present in the
    sibling _REL_SOURCE dict (used by /api/evidence), NOT in RELATION_INFO
    (used by this endpoint). So this rtype actually falls through to the
    exact same generic default as an unknown type -- title == rtype, not a
    curated title. Verified directly against RELATION_INFO before writing
    this assertion.
    """
    r = client.get("/api/relation-info", params={"rtype": "FELLOW_REPRESENTATIVE"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "FELLOW_REPRESENTATIVE"
    assert "desc" in body


def test_relation_info_nonexistent_type_returns_default(client):
    r = client.get("/api/relation-info", params={"rtype": "NONEXISTENT_TYPE"})
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "NONEXISTENT_TYPE"
    assert "desc" in body


def test_relation_info_response_time_under_500ms(client):
    t0 = time.perf_counter()
    r = client.get("/api/relation-info", params={"rtype": "COMMUNICATED_WITH"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 0.5, f"/api/relation-info took {elapsed:.3f}s (target < 500ms)"


# ==========================================================================
# 6. GET /api/category-info
# ==========================================================================

def test_category_info_family_high_probability(client):
    r = client.get("/api/category-info", params={"cat": "FAMILY"})
    assert r.status_code == 200
    body = r.json()
    assert body["probability"] > 0.8


def test_category_info_nonexistent_category_falls_back_to_other(client):
    r = client.get("/api/category-info", params={"cat": "NONEXISTENT_CAT"})
    assert r.status_code == 200
    body = r.json()
    assert body["probability"] == pytest.approx(0.05, abs=1e-6)


def test_category_info_response_time_under_500ms(client):
    t0 = time.perf_counter()
    r = client.get("/api/category-info", params={"cat": "FAMILY"})
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 0.5, f"/api/category-info took {elapsed:.3f}s (target < 500ms)"


# ==========================================================================
# 7. GET /api/methodology
# ==========================================================================

def test_methodology_returns_nonempty_text_mentioning_probability(client):
    r = client.get("/api/methodology")
    assert r.status_code == 200
    body = r.json()
    assert "text" in body
    assert len(body["text"]) > 0
    assert "probability" in body["text"].lower()


def test_methodology_response_time_under_500ms(client):
    t0 = time.perf_counter()
    r = client.get("/api/methodology")
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 0.5, f"/api/methodology took {elapsed:.3f}s (target < 500ms)"


# ==========================================================================
# Fuzz testing (Hypothesis property-based tests)
# ==========================================================================
#
# Property under test everywhere below: arbitrary text input, however
# adversarial, must never produce a 500 -- at worst a clean 4xx.
#
# All fuzz tests reuse the module's session-scoped `client` fixture rather
# than a function-scoped one. Hypothesis calls the test body many times
# per test *item*, and a function-scoped fixture only runs setup/teardown
# once per item -- so for most fixtures this silently skips re-isolation
# between examples, and for this specific `client` fixture it would also
# re-pay the ~20s cold graph load on every single generated example.
# Session scope is both the safe choice and the fast one here.

from hypothesis import HealthCheck, given, settings, strategies as st  # noqa: E402

_fuzz_text = st.text(alphabet=st.characters(blacklist_categories=('Cc', 'Cs')), min_size=0, max_size=100)


class TestApiFuzzing:

    @pytest.mark.timeout(120)  # 100 examples * ~0.8s/call (see below) > the module's 60s default
    @settings(deadline=None)
    @given(q=st.text(alphabet=st.characters(blacklist_categories=('Cc', 'Cs')), min_size=0, max_size=100))
    def test_search_fuzzing(self, client, q):
        """Search handles arbitrary strings without crashing (500).

        deadline=None: _find_entry scans the full ~240K-entry search index
        per call (~0.7-0.9s measured, see the 2s SLA test above) -- well
        past Hypothesis's default 200ms per-example deadline, which is
        unrelated to any actual hang.
        """
        response = client.get(f"/api/search?q={q}")
        assert response.status_code in (200, 422)  # 422 for validation, never 500
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, list)

    @settings(max_examples=25, deadline=None)
    @given(src=_fuzz_text, tgt=_fuzz_text)
    def test_path_fuzzing(self, client, src, tgt):
        """/api/path handles arbitrary src_name/tgt_name strings without crashing (500).

        Capped at 25 examples (Hypothesis's default is 100) and with the
        per-example deadline disabled: a src/tgt pair that actually
        resolves to real graph nodes triggers a genuine ~7-10s
        shortest_simple_paths search (see the module docstring above), so
        an unbounded run here buys many extra minutes for essentially no
        extra coverage -- nearly every generated string fails to resolve
        to a node and returns immediately via the `_resolve_name` miss.
        """
        response = client.get("/api/path", params={"src_name": src, "tgt_name": tgt})
        assert response.status_code in (200, 422)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert "error" in data or "paths" in data

    @given(src=_fuzz_text, tgt=_fuzz_text, rel=_fuzz_text)
    def test_evidence_fuzzing(self, client, src, tgt, rel):
        """/api/evidence handles arbitrary src/tgt/rel strings without crashing (500)."""
        response = client.get("/api/evidence", params={"src": src, "tgt": tgt, "rel": rel})
        assert response.status_code in (200, 422)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert isinstance(data.get("evidence"), list)

    @given(rtype=_fuzz_text)
    def test_relation_info_fuzzing(self, client, rtype):
        """/api/relation-info handles arbitrary rtype strings without crashing (500)."""
        response = client.get("/api/relation-info", params={"rtype": rtype})
        assert response.status_code in (200, 422)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert "title" in data

    @given(cat=_fuzz_text)
    def test_category_info_fuzzing(self, client, cat):
        """/api/category-info handles arbitrary cat strings without crashing (500)."""
        response = client.get("/api/category-info", params={"cat": cat})
        assert response.status_code in (200, 422)
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, dict)
            assert isinstance(data.get("probability"), (int, float))

def test_anonymous_session_tracking(client):
    import sqlite3
    client.cookies.clear()
    # Check if request sets a session_id cookie
    response = client.get("/api/search", params={"q": "trump"})
    assert response.status_code == 200
    
    # Verify cookie was set
    session_id = response.cookies.get("session_id") or client.cookies.get("session_id")
    assert session_id is not None
    assert len(session_id) > 0
    
    # Query database to check if event was logged
    db_path = pf.DATA_DIR / "ops_metrics.db"
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT session_id, event_type, metadata FROM anonymous_events WHERE session_id = ?", (session_id,))
    rows = cur.fetchall()
    conn.close()
    
    assert len(rows) > 0
    assert rows[0][1] == "request"
    metadata = json.loads(rows[0][2])
    assert metadata["path"] == "/api/search"
