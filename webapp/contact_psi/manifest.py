"""Offline OPRF-keyed AES-GCM manifest construction and lookup."""
from __future__ import annotations
import base64
import gzip
import hashlib
import json
import os
import logging
from collections import defaultdict
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from . import keys, oprf

logger = logging.getLogger(__name__)
MAX_MANIFEST_BYTES = 150 * 1024 * 1024
POSSIBLE_TIER_MAX_ENTITIES = None
MAX_BUNDLE_MATCHES = 20

def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")

def _items(name: str):
    exact = keys.normalize_exact(name)
    if exact:
        yield "exact", exact
    phonetic = keys.phonetic_key(name)
    if phonetic:
        yield "phonetic", phonetic
    for token in keys.lsh_band_tokens(name):
        yield "possible", token

def build_manifest(db_records, secret_scalar: bytes, key_version: int):
    """Build a JSON-compatible manifest and return ``(manifest, entry_count)``."""
    groups = defaultdict(list)
    records = list(db_records)
    if POSSIBLE_TIER_MAX_ENTITIES is not None:
        records = records[:POSSIBLE_TIER_MAX_ENTITIES]
    for record in records:
        name = str(record.get("name", "")).strip()
        if not name:
            continue
        match = {"id": record.get("id"), "name": name}
        for tier, item in _items(name):
            groups[(tier, item)].append((record.get("score", 0) or 0, match))

    buckets = defaultdict(list)
    for (tier, item), matches in groups.items():
        matches = [m for _, m in sorted(matches, key=lambda pair: pair[0], reverse=True)[:MAX_BUNDLE_MATCHES]]
        key = oprf.full_eval(secret_scalar, key_version, tier, item)
        payload = json.dumps({"tier": tier, "matches": matches}, separators=(",", ":"), ensure_ascii=False).encode()
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(nonce, payload, None)
        buckets[key[:4].hex()].append({"nonce": _b64(nonce), "ct": _b64(ciphertext)})
    manifest = {"version": 1, "key_version": int(key_version), "buckets": dict(buckets)}
    return manifest, sum(len(v) for v in buckets.values())

def manifest_bytes(manifest) -> bytes:
    return json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode()

def manifest_size_bytes(manifest) -> int:
    # Raw serialized size is intentionally exposed for diagnostics/tests.  The
    # production gate below uses the actual compressed artifact size.
    return len(manifest_bytes(manifest))

def enforce_size_budget(manifest) -> int:
    size = len(gzip.compress(manifest_bytes(manifest), compresslevel=9))
    if size > MAX_MANIFEST_BYTES:
        raise ValueError(f"contact PSI manifest exceeds {MAX_MANIFEST_BYTES} bytes: {size}")
    return size

def save_manifest(manifest, path) -> int:
    enforce_size_budget(manifest)
    data = gzip.compress(manifest_bytes(manifest), compresslevel=9)
    with open(path, "wb") as handle:
        handle.write(data)
    return len(data)

def load_manifest(path):
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        data = gzip.decompress(data)
    except OSError:
        pass
    return json.loads(data)

# ---------------------------------------------------------------------------
# Prefix-range sharding.
#
# A single manifest covering the real ~1.46M-person corpus comes out to
# ~682MB compressed -- ~4.5x MAX_MANIFEST_BYTES, and capping the corpus to
# fit would mean excluding real people from ever being matchable. Since a
# lookup only ever needs ONE bucket (keyed by a 4-byte/8-hex-char prefix of
# an OPRF-derived key), the manifest can instead be split into shards by
# the first `shard_hex_chars` hex characters of that prefix, and the client
# fetches only the shard(s) that actually cover the prefixes it derived --
# typically a handful of shards per contact-list check, not the whole
# corpus. SHA-256-derived prefixes are uniformly distributed, so shards
# come out roughly even in size (no single shard should approach the
# per-shard budget in practice).
#
# This does add a small, bounded fetch-pattern leak versus the single-file
# design: an observer who sees which shard URL(s) a client requests learns
# `shard_hex_chars * 4` bits of a derived key's hash prefix (default: 8
# bits, i.e. 1-of-256) per item queried -- not the item itself, not even
# close to enough to invert. This is the same category of tradeoff
# HaveIBeenPwned's breach-check API makes deliberately (that API shares a
# 20-bit hash prefix; this shares fewer bits, so it leaks less).
SHARD_HEX_CHARS = 2  # 2 hex chars = 1 byte = 256 shards

def shard_id_for_prefix(prefix_hex: str, shard_hex_chars: int = SHARD_HEX_CHARS) -> str:
    return prefix_hex[:shard_hex_chars]

def shard_filename(shard_id: str) -> str:
    return f"contact_psi_manifest_{shard_id}.json.gz"

def save_sharded_manifest(manifest, output_dir, shard_hex_chars: int = SHARD_HEX_CHARS) -> dict:
    """Split `manifest`'s buckets by prefix into per-shard gzip'd JSON files
    under `output_dir`. Returns {shard_id: compressed_size_bytes} for every
    shard actually written (empty shards are not written)."""
    os.makedirs(output_dir, exist_ok=True)
    grouped = defaultdict(dict)
    for prefix, entries in manifest.get("buckets", {}).items():
        grouped[shard_id_for_prefix(prefix, shard_hex_chars)][prefix] = entries

    sizes = {}
    for shard_id, buckets in grouped.items():
        shard_manifest = {
            "version": manifest.get("version", 1),
            "key_version": int(manifest["key_version"]),
            "shard": shard_id,
            "buckets": buckets,
        }
        data = gzip.compress(manifest_bytes(shard_manifest), compresslevel=9)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError(f"contact PSI manifest shard {shard_id} exceeds {MAX_MANIFEST_BYTES} bytes: {len(data)}")
        with open(os.path.join(output_dir, shard_filename(shard_id)), "wb") as handle:
            handle.write(data)
        sizes[shard_id] = len(data)
    return sizes

def load_shard(output_dir, shard_id):
    path = os.path.join(output_dir, shard_filename(shard_id))
    with open(path, "rb") as handle:
        data = handle.read()
    try:
        data = gzip.decompress(data)
    except OSError:
        pass
    return json.loads(data)

def lookup(manifest, aes_key: bytes) -> list[dict]:
    results = []
    for entry in manifest.get("buckets", {}).get(aes_key[:4].hex(), []):
        try:
            plaintext = AESGCM(aes_key).decrypt(base64.b64decode(entry["nonce"]), base64.b64decode(entry["ct"]), None)
            results.append(json.loads(plaintext))
        except Exception:
            continue
    return results
