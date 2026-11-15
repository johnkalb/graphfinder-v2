"""Compact contact-matching manifest (format 2, 2026-11-15): builder round
trip and the two endpoints that serve it."""
import os

import pytest

from test_tester_api import client, pf, tester_data_dir  # noqa: F401

from contact_psi import keys, manifest, oprf

pysodium = pytest.importorskip("pysodium")


@pytest.fixture(scope="module")
def built():
    secret = pysodium.crypto_core_ristretto255_scalar_random()
    recs = [{"id": i, "name": n, "score": s} for i, (n, s) in enumerate(
        [("Mark Greene", 5), ("Mark Greene (Ibm)", 9), ("Jane Doe", 3)] + [(f"Filler Person{i}", 1) for i in range(300)])]
    exact, fuzzy, stats = manifest.build_compact(recs, secret, 3)
    return secret, exact, fuzzy, stats


def test_round_trip_namesakes_and_non_matches(built):
    secret, exact, fuzzy, stats = built
    assert len(exact) == manifest.EXACT_PARTS and stats["exact_entries"] >= 303
    key = oprf.full_eval(secret, 3, "exact", keys.normalize_exact("Mark Greene"))
    names = [m["name"] for m in manifest.lookup_compact(exact[manifest.exact_part(key)], key)]
    assert names == ["Mark Greene (Ibm)", "Mark Greene"]          # best-known namesake first
    miss = oprf.full_eval(secret, 3, "exact", keys.normalize_exact("Nobody Here"))
    assert manifest.lookup_compact(exact[manifest.exact_part(miss)], miss) == []
    pk = oprf.full_eval(secret, 3, "phonetic", keys.phonetic_key("Jane Do"))
    assert [m["name"] for m in manifest.lookup_compact(fuzzy[pk[:1].hex()], pk)] == ["Jane Doe"]
    # nothing readable without the key: no names in the files
    assert b"Greene" not in b"".join(exact) and b"Jane" not in b"".join(fuzzy.values())


def test_endpoints_serve_parts_and_padded_fuzzy_batches(client, built, tester_data_dir):
    _, exact, fuzzy, _ = built
    out = os.path.join(tester_data_dir, "contact_psi_manifest")
    manifest.save_compact(out, exact, fuzzy)
    r = client.get("/api/contacts/manifest-exact/0?v=x")
    assert r.status_code == 200 and r.content == exact[0] and "immutable" in r.headers["cache-control"]
    assert client.get(f"/api/contacts/manifest-exact/{manifest.EXACT_PARTS}").status_code == 404
    some = sorted(fuzzy)[:2]
    r = client.post("/api/contacts/manifest-fuzzy", json={"shards": some + ["zz"]})
    assert r.status_code == 400
    r = client.post("/api/contacts/manifest-fuzzy", json={"shards": some})
    body, o, got = r.content, 0, {}
    while o < len(body):
        sid, n = f"{body[o]:02x}", int.from_bytes(body[o + 1:o + 5], "little")
        got[sid] = body[o + 5:o + 5 + n]
        o += 5 + n
    assert got == {s: fuzzy[s] for s in some}
    assert client.post("/api/contacts/manifest-fuzzy", json={"shards": ["00"] * 65}).status_code == 400
