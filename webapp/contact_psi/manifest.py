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
# Exact-tier bundles hold every same-named node -- common names are split by
# employer into "Michael Smith (Goldman Sachs)" etc. (disambiguation.py), and
# the browser picks between them using the contact's LinkedIn company, so a
# low cap would hide the right one behind higher-degree namesakes.
MAX_EXACT_BUNDLE_MATCHES = 200

def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")

def base_name(name: str) -> str:
    """'Wei Wang (Tencent Technology (Shenzhen) Company Limited)' -> 'Wei Wang';
    names without a trailing parenthetical come back unchanged."""
    name = name.strip()
    i = name.find(" (")
    return name[:i].strip() if name.endswith(")") and i > 0 else name

def _items(name: str):
    exact = keys.normalize_exact(name)
    if exact:
        yield "exact", exact
    # A split namesake is also findable by the bare name a contact list holds.
    base = base_name(name)
    if base != name:
        base_exact = keys.normalize_exact(base)
        if base_exact and base_exact != exact:
            yield "exact", base_exact
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
        cap = MAX_EXACT_BUNDLE_MATCHES if tier == "exact" else MAX_BUNDLE_MATCHES
        matches = [m for _, m in sorted(matches, key=lambda pair: pair[0], reverse=True)[:cap]]
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

# ---------------------------------------------------------------------------
# Compact format (format 2, 2026-11-15).
#
# Why: with 256 JSON shards, any upload of more than ~200 contacts needed
# nearly every shard (~1 GB in the browser), and splitting into smaller shards
# would let whoever runs the server -- who holds the OPRF secret and so knows
# which names land in which shard -- narrow each matched contact down to a
# handful of names from the shard request pattern alone.
#
# So the EXACT tier (what nearly every match comes from) is one compact binary
# file that every browser downloads whole and caches: the fetch reveals
# nothing about which contacts were checked. The fuzzy tiers (phonetic + LSH)
# are 256 compact shards fetched only for small uploads, padded with random
# shards by the browser.
#
# Layout (all integers little-endian):
#   b"PSIX" | u8 format=2 | u32 key_version | u32 N
#   N x 6-byte key prefixes, sorted ascending (binary-searchable)
#   (N + 1) x u32 offsets into the blob (entry i = blob[off[i]:off[i+1]])
#   blob: AES-GCM ciphertexts with the tag truncated to 12 bytes (96-bit,
#         allowed by GCM and WebCrypto's tagLength); nonce = first 12 bytes
#         of sha256(b"contact-psi-nonce-v1" + key) -- each key encrypts once
#   plaintext: the matching graph names, best first, one per line
# Fuzzy files hold only the phonetic tier: the LSH "possible" tier was most
# of the volume and its matches were never addable in the UI.
# ---------------------------------------------------------------------------
import struct

COMPACT_MAGIC = b"PSIX"
COMPACT_FORMAT = 2
COMPACT_PREFIX_BYTES = 6
COMPACT_TAG_BYTES = 12
FUZZY_TIERS = ("phonetic",)
EXACT_PARTS = 4                     # by the key's first byte (0-63, 64-127, ...): keeps each file
                                    # under GitHub's 100 MB limit; the browser always fetches all parts


def exact_filename(part: int) -> str:
    return f"contact_psi_exact_{part}.bin"


def exact_part(key: bytes) -> int:
    return key[0] * EXACT_PARTS // 256


def fuzzy_filename(shard_id: str) -> str:
    return f"contact_psi_fuzzy_{shard_id}.bin"


def compact_nonce(key: bytes) -> bytes:
    return hashlib.sha256(b"contact-psi-nonce-v1" + key).digest()[:12]


def _payload(matches) -> bytes:
    return "\n".join(m["name"].replace("\n", " ").replace("\r", " ") for m in matches).encode("utf-8")


def compact_groups(db_records):
    """{(tier, item): [match dicts, best first, capped]} for every record."""
    groups = defaultdict(list)
    for record in db_records:
        name = str(record.get("name", "")).strip()
        if not name:
            continue
        match = {"id": record.get("id"), "name": name}
        for tier, item in _items(name):
            groups[(tier, item)].append((record.get("score", 0) or 0, match))
    out = {}
    for (tier, item), matches in groups.items():
        cap = MAX_EXACT_BUNDLE_MATCHES if tier == "exact" else MAX_BUNDLE_MATCHES
        out[(tier, item)] = [m for _, m in sorted(matches, key=lambda p: p[0], reverse=True)[:cap]]
    return out


def encode_compact(entries, key_version: int) -> bytes:
    """entries: [(key32, plaintext_bytes)] -> the binary file."""
    rows = []
    for key, plaintext in entries:
        ct = AESGCM(key).encrypt(compact_nonce(key), plaintext, None)[:len(plaintext) + COMPACT_TAG_BYTES]
        rows.append((key[:COMPACT_PREFIX_BYTES], ct))
    rows.sort(key=lambda r: r[0])
    offsets, pos = [], 0
    for _, ct in rows:
        offsets.append(pos)
        pos += len(ct)
    offsets.append(pos)
    head = COMPACT_MAGIC + struct.pack("<BII", COMPACT_FORMAT, int(key_version), len(rows))
    return b"".join([head, b"".join(p for p, _ in rows), struct.pack(f"<{len(offsets)}I", *offsets),
                     b"".join(ct for _, ct in rows)])


def build_compact(db_records, secret_scalar: bytes, key_version: int, progress=None):
    """-> ([exact part bytes x EXACT_PARTS], {shard_id: fuzzy file bytes}, stats)."""
    groups = compact_groups(db_records)
    exact, fuzzy = [], defaultdict(list)
    for i, ((tier, item), matches) in enumerate(groups.items()):
        if tier != "exact" and tier not in FUZZY_TIERS:
            continue
        key = oprf.full_eval(secret_scalar, key_version, tier, item)
        entry = (key, _payload(matches))
        if tier == "exact":
            exact.append(entry)
        else:
            fuzzy[key[:1].hex()].append(entry)
        if progress and i % 200_000 == 0:
            progress(i, len(groups))
    parts = defaultdict(list)
    for e in exact:
        parts[exact_part(e[0])].append(e)
    exact_bytes = [encode_compact(parts[i], key_version) for i in range(EXACT_PARTS)]
    fuzzy_bytes = {sid: encode_compact(e, key_version) for sid, e in fuzzy.items()}
    return exact_bytes, fuzzy_bytes, {"exact_entries": len(exact), "fuzzy_entries": sum(map(len, fuzzy.values())),
                                      "exact_bytes": sum(map(len, exact_bytes)),
                                      "fuzzy_bytes": sum(map(len, fuzzy_bytes.values()))}


def decode_compact(data: bytes):
    """-> (key_version, prefixes list, offsets list, blob memoryview)."""
    if data[:4] != COMPACT_MAGIC:
        raise ValueError("not a compact PSI file")
    fmt, kv, n = struct.unpack_from("<BII", data, 4)
    if fmt != COMPACT_FORMAT:
        raise ValueError(f"unknown compact format {fmt}")
    p0 = 13
    prefixes = [data[p0 + i * COMPACT_PREFIX_BYTES: p0 + (i + 1) * COMPACT_PREFIX_BYTES] for i in range(n)]
    o0 = p0 + n * COMPACT_PREFIX_BYTES
    offsets = list(struct.unpack_from(f"<{n + 1}I", data, o0))
    return kv, prefixes, offsets, memoryview(data)[o0 + (n + 1) * 4:]


def lookup_compact(data: bytes, aes_key: bytes) -> list[dict]:
    """The matches for one derived key (what the browser does), for tests."""
    import bisect
    _kv, prefixes, offsets, blob = decode_compact(data)
    p = aes_key[:COMPACT_PREFIX_BYTES]
    i = bisect.bisect_left(prefixes, p)
    out = []
    while i < len(prefixes) and prefixes[i] == p:
        ct = bytes(blob[offsets[i]:offsets[i + 1]])
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            d = Cipher(algorithms.AES(aes_key), modes.GCM(compact_nonce(aes_key), ct[-COMPACT_TAG_BYTES:],
                                                          min_tag_length=COMPACT_TAG_BYTES)).decryptor()
            pt = d.update(ct[:-COMPACT_TAG_BYTES]) + d.finalize()
            out += [{"name": n} for n in pt.decode("utf-8").split("\n") if n]
        except Exception:
            pass
        i += 1
    return out


def save_compact(output_dir, exact_bytes: list, fuzzy_bytes: dict):
    """Write the exact file and fuzzy shards, removing files of the old JSON format."""
    os.makedirs(output_dir, exist_ok=True)
    for f in os.listdir(output_dir):
        if f.startswith(("contact_psi_manifest_", "contact_psi_fuzzy_", "contact_psi_exact")):
            os.remove(os.path.join(output_dir, f))
    for i, data in enumerate(exact_bytes):
        with open(os.path.join(output_dir, exact_filename(i)), "wb") as h:
            h.write(data)
    for sid, data in fuzzy_bytes.items():
        with open(os.path.join(output_dir, fuzzy_filename(sid)), "wb") as h:
            h.write(data)
