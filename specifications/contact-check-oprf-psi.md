# Feature: Contact-Check via OPRF Private Set Intersection

Replaces the existing `/api/psi` endpoint and client-side fuzzy-match flow in
`webapp/pathfinder.py` (the "🔒 Check My Contacts" button) with a
cryptographically sound design. **Do not reuse `_load_psi_names` /
`psi_match` / the client-supplied-salt scheme currently in `pathfinder.py`
(lines ~694-736) — it is being replaced, not extended, because it does not
actually protect user contact privacy (see "Why the old design is broken"
below).**

## Requirement (from the operator)

Three invariants, all required simultaneously:

1. **Zero outbound leakage** — the server must never receive the user's
   contacts.
2. **Anti-bulk-scraping** — a client (including an authorized-but-malicious
   one) must not be able to reconstruct the ~600,000 names in the database.
3. **Latency** — end-to-end check must complete in well under 10 seconds for
   a realistic contact list (hundreds to a few thousand entries).

## Why the naive "encrypted index" design is broken

An earlier draft of this feature proposed: derive a symmetric key directly
from a hash of each name (`K = SHA256(normalize(name))` for exact match,
`K = SHA256(metaphone(name))` for phonetic), encrypt a small payload under
`K`, and publish the whole encrypted table for offline client-side lookup.

This does not achieve invariant 2. `K` is deterministic and computable from
public information only — a name. An attacker downloads the public table
once, then takes any large public corpus of candidate names (e.g. all
Wikidata person labels — which heavily overlaps this DB's own source
material) and tries decrypting every entry with `SHA256(candidate)`. GCM's
authentication tag gives a free success/fail oracle. This recovers most of
the "exact" and essentially all of the "phonetic" tier (its keyspace,
Soundex/Metaphone codes, is small enough to enumerate exhaustively) —
entirely offline, with no further contact with the server. **Convergent
encryption only hides a plaintext when the plaintext has enough entropy that
guessing it is infeasible. Human names don't have that entropy, especially
not in a domain (public figures) where the candidate universe is itself
public.** No amount of hashing changes that. Zero-server-interaction and
anti-scraping are not simultaneously achievable for low-entropy data without
a server-held secret in the loop.

Also, a `MinHash` signature (a *set* of per-permutation minimums, used for
similarity comparison) is not a single deterministic value and cannot serve
as an AES key the way the earlier draft described — that tier wasn't
implementable as written, independent of the security issue.

## The fix: OPRF-based PSI (same pattern as Signal/Apple/Google private
## contact discovery)

The server holds a persistent secret scalar `s` that never leaves it. All
per-item keys are `Eval_s(item) = s * H1(item)` — a value that requires
knowledge of `s` to compute, so an attacker without `s` cannot precompute
anything offline no matter how good their name-guessing corpus is. The
client obtains `Eval_s(item)` **only for items it already possesses**, via a
*blinded* evaluation: it never reveals the item to the server, and the
server's response, on its own, reveals nothing to an eavesdropper either
(indistinguishable from random under the DDH assumption on ristretto255).
Because deriving a usable key requires one interactive call per batch, bulk
enumeration requires the attacker to actually query the live server —
which can be rate-limited, unlike an offline-attackable static file.

This is a combination of two standard, well-studied primitives:
- **DH-OPRF blinding** (2HashDH, non-verifiable — no need for the
  proof-of-correct-exponentiation that full VOPRF/RFC 9497 adds, since our
  threat model is "server shouldn't learn queries," not "client shouldn't
  trust a malicious server's math").
- **Labeled PSI via OPRF-keyed AEAD** — once a client has `Eval_s(item)`
  for one of its own items, it derives a symmetric key from it and can
  decrypt an associated payload (the matched DB name, entity id, etc.) if
  and only if that item is actually in the DB. This is the same combined
  pattern used by Google Password Checkup / credential-leak-check services.

### Protocol

Group: **ristretto255** (RFC 9496). Server: Python via `pysodium` (already
verified installable — `pip install pysodium`, wraps libsodium's
`crypto_core_ristretto255_*` / `crypto_scalarmult_ristretto255`). Client:
`@noble/curves` (`@noble/curves/ed25519` exposes `RistrettoPoint`) — pure
JS/TS, audited, no WASM/native dependency, runs directly in the browser.
Both implement the same published ristretto255 spec, so points and
encodings are interoperable by construction.

```
H1(namespace, item) = ristretto255_from_hash( SHA512(namespace || 0x00 || item) )
```
`namespace ∈ {"exact", "phonetic", "possible"}` — domain separation so the
same underlying string can't collide across tiers.

**Server setup (one-time, and on rotation):**
- `s = crypto_core_ristretto255_scalar_random()` — 32 random bytes reduced
  mod the group order. Stored only in the server secrets store (e.g.
  `CONTACT_PSI_SERVER_SECRET`, base64, env var / secrets manager). **Never
  logged, never committed, never included in any manifest or response.**
- `key_version` — integer, starts at 1, incremented on every rotation.

**Manifest build (offline batch job, runs as part of the existing nightly
"refresh scored graph" pipeline — see Integration section):**
For every DB person-entity name and every tier's derived item string:
```
point      = H1(namespace, item)
oprf_point = s * point                     # crypto_scalarmult_ristretto255(s, point)
aes_key    = SHA256("contact-psi-v1" || key_version(4B BE) || oprf_point(32B))[:32]
payload    = JSON{tier, matches: [{id, name}, ...]}   # see dedup note below
nonce      = random 12 bytes
ciphertext = AES-256-GCM-Encrypt(aes_key, nonce, payload)
```
Store `(prefix = aes_key[:4], nonce, ciphertext)` in the manifest, bucketed
by `prefix` (hex string key) for O(1) average client lookup.

**Dedup requirement:** when multiple DB names produce the *same* item string
for a given tier (e.g. many surnames share a phonetic code, or two names
land in the same LSH band), they **must** be bundled into one manifest entry
whose payload lists all of them — not one ciphertext per name. This is what
keeps the phonetic/possible tiers from blowing up payload size, since those
tiers are inherently many-to-one. Cap the bundle size (e.g. top 20 by
existing SCI/degree score) so a single very common code can't balloon one
entry.

**Client query (`POST /api/contacts/oprf-eval`):**
```
request:  { "key_version": N, "points": [b64(32B point), ...] }
response: { "key_version": N, "points": [b64(32B point), ...] }   # response[i] = s * request.points[i]
```
This endpoint's request/response schema **must contain no name/string field
of any kind** — only opaque base64 curve points and an integer version. See
Test Suite § schema-leakage tests, which enforce this via introspection so a
future "helpful" addition of a plaintext fallback fails CI.

Client-side, per item:
```
r        = random scalar (fresh per item, never reused, never persisted)
blinded  = r * H1(namespace, item)                 # sent to server
response = s * blinded  (== s*r*H1(item), from server)
K        = r^-1 * response  (== s * H1(item), same value server used to build the manifest)
aes_key  = SHA256("contact-psi-v1" || key_version(4B BE) || K(32B))[:32]
```
Look up `aes_key[:4]` in the downloaded manifest's bucket index; attempt
AES-GCM decrypt on each candidate ciphertext in that bucket (wrong-key
attempts simply fail the auth tag — this is normal, expected, not an error).

**Manifest distribution:** static file (e.g.
`webapp/data/contact_psi_manifest.bin` or gzip'd JSON — implementer's
choice as long as the format is documented and the size budget below is
met), served over the existing CDN/Cloudflare path, cached client-side
(Service Worker or IndexedDB) keyed by `key_version` + content hash. A
lightweight `GET /api/contacts/manifest-meta` returns
`{key_version, manifest_url, sha256, size_bytes, built_at}` so the client
can detect staleness and re-fetch without silently producing wrong results
against a stale manifest.

### Tier key derivation

**Exact:** `normalize_exact(name)` — NFKD-fold to ASCII (strip diacritics),
lowercase, replace `-`/`'`/`.`/`,` with space, strip remaining
non-alphanumeric, drop a trailing generational suffix token
(`jr/sr/ii/iii/iv/v`), split into tokens, **sort tokens alphabetically**
(word-order independence — `"Smith, John"` and `"John Smith"` normalize
identically), rejoin with single spaces.

**Phonetic ("Likely" tier):** **Refined Soundex, not Double Metaphone.**
Double Metaphone was the original draft's choice, but different language
ports of Double Metaphone are known to diverge on edge cases, and this
protocol requires the server-side (Python, batch build) and client-side
(browser JS) phonetic computation to be **bit-identical** — any divergence
is a silent false negative (a real match that never surfaces, with no error
to signal it). Soundex is ~15 lines, unambiguous, and trivially portable
without risk of edge-case drift. We use a "refined" variant that classes
the *first* letter too (textbook Soundex keeps letter 1 literal, which is
why it famously fails on `Katherine`/`Catherine` — both are consonant class
2, but textbook Soundex still emits `K365` vs `C365`). Full algorithm:

```
class(c):
  b,f,p,v       -> "1"
  c,g,j,k,q,s,x,z -> "2"
  d,t           -> "3"
  l             -> "4"
  m,n           -> "5"
  r             -> "6"
  a,e,i,o,u,y   -> "0"     # vowel: breaks adjacent-duplicate suppression
  h,w           -> (transparent: skip, does not update "last class")

soundex_token(token):
  letters = [c for c in token.lower() if c is a-z]
  if empty: return "0000"
  digits = []
  last_class = None
  for c in letters:
    if c in {h, w}: continue                 # transparent
    cls = class(c)                            # "0".."6"
    if cls != last_class:
      digits.append(cls)
      if len(digits) == 4: break
    last_class = cls
  pad digits with "0" to length 4
  return "".join(digits)                      # exactly 4 chars, e.g. "2050"
```
`phonetic_key(name)` = run `normalize_exact` first (shares tokenization /
suffix-stripping), take each token, run `soundex_token`, **sort the
resulting codes alphabetically**, join with `"|"`. Empty string (no letters
at all) means "no phonetic tier for this name" — skip it, don't query it.

Known, accepted limitation: this tier is sensitive to *token count*
(`"Donald J. Trump"` vs `"Donald Trump"` — the middle initial adds a third
token and does not collapse away, so these do **not** match at the phonetic
tier). The "Possible" tier below still catches such cases via trigram
overlap. Do not attempt to special-case middle initials in v1.

**Possible tier (trigram MinHash + LSH banding):**
```
trigrams(name) = sliding 3-char windows over " " + normalize_exact(name) + " ", as a SET (dedup)
```
32 fixed `(a, b)` coefficient pairs define 32 hash functions over a
Mersenne prime `P = 2^61 - 1`:
```
hash_i(trigram) = (a_i * fnv1a32(trigram) + b_i) mod P
```
`fnv1a32` is the standard 32-bit FNV-1a hash (offset basis `0x811C9DC5`,
prime `0x01000193`) over the trigram's UTF-8 bytes.

**Use exactly 24 of the 32 coefficient pairs, arranged as 3 bands of 8
rows each** (`BANDS = 3`, `ROWS_PER_BAND = 8`). The full 32-entry
coefficient table (only the first 24 are used; all 32 are provided in case
a future revision widens banding) is machine-generated once via a fixed
xorshift64 seed and then **pinned as a literal constant** — see
`contact-check-psi-test-vectors.json` in this directory for the exact
values every implementation must hardcode (do not regenerate this table;
copy it verbatim into both the Python build script and the JS client).

```
minhash_signature(name) = for each of the 24 (a,b) pairs: min(hash_i(t) for t in trigrams(name))
                           (if trigrams(name) is empty, signature is 24 zeros)

lsh_band_tokens(name) = for band_idx in 0..2:
    chunk = signature[band_idx*8 : band_idx*8+8]
    token = f"{band_idx}:" + ",".join(str(v) for v in chunk)
  -> list of exactly 3 token strings, each queried as its own "possible"-namespace OPRF item
```

A match at this tier means the client's contact shares at least one LSH
band with a DB name — i.e. probably-similar, not certainly-similar. **After
decrypting a "possible"-tier hit, the client must locally recompute Jaccard
similarity between its own contact's trigram set and the decrypted DB
name's trigram set (ship the DB name's trigram *count* or a small sketch in
the payload, not the full set, to keep payload size down — implementer's
choice of what's sufficient to recompute a similarity score) and discard the
match if similarity is below 0.5.** This filters band-collision false
positives before they ever reach the results UI.

### Match ranking

Per contact, if multiple tiers hit, keep only the highest-confidence one:
`exact > phonetic > possible`. Label them "Definite" / "Likely" / "Possible"
in the UI (matching the operator's original three-tier naming).

### Rate limiting

The whole site is already gated to ~10 named users via Cloudflare Zero
Trust Access (see `README.md`), which forwards an authenticated-email
header the codebase already reads elsewhere (see the commit that added
"FastAPI endpoints with Cloudflare authenticated email header capture").
Use that header as the rate-limit key.

- Max **3,000 points per single `/api/contacts/oprf-eval` request** (client
  chunks larger contact lists into multiple sequential batched requests).
- Max **5,000 total OPRF-eval points per authenticated user per rolling
  24h window.** Generous for a real address book (hundreds to low
  thousands of contacts × up to 5 items/contact = comfortably inside
  budget for one legitimate check, even run a few times a day) but nowhere
  near enough to brute-force a meaningful fraction of a 600k-entry corpus.
- Store counters in the existing `webapp/data/ops_metrics.db` (already used
  by `ops_monitor.py` for request logging) — one row/counter per user per
  UTC day, incremented per request, checked before evaluating.
- Exceeding the limit returns HTTP 429 with a `Retry-After` header.

### Non-functional requirements

- `/api/contacts/oprf-eval` must **never log request or response bodies**
  (no debug logging of points — they're not secret against a passive
  eavesdropper under DDH, but logging them is unnecessary surface area and
  bad hygiene for a privacy feature).
- No plaintext name/contact field may appear anywhere in this endpoint's
  request or response Pydantic models, now or in any future change.
- Server secret `s` rotation: recommended every 90 days or on suspected
  compromise. Rotation requires a full manifest rebuild (fits the existing
  nightly cron cadence — this pipeline already rebuilds derived data daily
  per the "Refresh scored graph from enriched DB" commits). Bump
  `key_version` on rotation; old cached client manifests become
  undecryptable (expected — client detects via `manifest-meta` and
  re-fetches).
- Manifest build must print/log actual entry counts, bucket count, and
  final compressed size. There is no way to hand-derive an exact payload
  size without running the real 600k-entry corpus through the pipeline
  (tier dedup rates depend on real name-distribution data this spec
  doesn't have access to) — **treat payload size as a build-time
  measurement with a hard gate, not a fixed number**: fail the build (and
  the corresponding test in the test suite) if compressed manifest size
  exceeds **150 MB**. If the real build comes in over budget, first lever
  to pull is capping the "possible" tier to DB entities above some
  SCI/degree percentile (e.g. top 100k by existing Social Capital Index
  score) rather than the full 600k — fuzzy/trigram matching against very
  obscure long-tail entities has low practical value for this feature
  anyway. Expose this as a build-time constant
  (`POSSIBLE_TIER_MAX_ENTITIES`) so it's a one-line tuning knob, not a
  design change.

### Required file/module layout (so the test suite has stable import targets)

```
webapp/contact_psi/__init__.py
webapp/contact_psi/keys.py       # normalize_exact, phonetic_key, trigrams,
                                  # minhash_signature, lsh_band_tokens,
                                  # MINHASH_COEFFICIENTS (the pinned 32-pair table)
webapp/contact_psi/oprf.py       # h1, new_server_secret, eval_s, new_blind_scalar,
                                  # blind, unblind, derive_aes_key
webapp/contact_psi/manifest.py   # build_manifest(db_records, secret_scalar, key_version),
                                  # load_manifest(path), lookup(manifest, prefix)
build_contact_psi_manifest.py    # CLI entry point, part of the nightly pipeline
```
`webapp/pathfinder.py` imports from `contact_psi` and adds the two new
routes (`POST /api/contacts/oprf-eval`, `GET /api/contacts/manifest-meta`)
plus removes `_load_psi_names`/`psi_match`/`/api/psi`. Keep `webapp/contact_psi/`
as plain, framework-free Python (no FastAPI imports inside it) so it's
testable in isolation from the web layer, matching how `link_scoring.py`
and `relation_categories.py` are already factored out of `pathfinder.py`.

### Integration points

- Replace `_load_psi_names` / `psi_match` / `/api/psi` in
  `webapp/pathfinder.py` with the new `/api/contacts/oprf-eval` and
  `/api/contacts/manifest-meta` endpoints.
- Replace the client-side `doPSI` function and the fuzzy-match-against-
  `/api/names` flow (the plaintext `compact_names.json.gz` download) with
  the new blind-query + manifest-decrypt pipeline. **`/api/names` (which
  serves the full plaintext name list for client-side fuzzy matching) must
  be removed or restricted once this ships** — it is the exact bulk-
  scraping hole this whole feature exists to close; leaving it live
  defeats the point.
- Manifest build step is a new script (e.g. `build_contact_psi_manifest.py`)
  invoked from the same pipeline that already runs `build_index.py` /
  `build_scored_edges.py` nightly.
- The "🔒 Check My Contacts" button, file input, and privacy note in the
  homepage HTML stay as-is; only the JS behind them changes.

### Known-answer test vectors

`contact-check-psi-test-vectors.json` (this directory) contains, for a
fixed deterministic test secret (derived as
`SHA512("contact-psi-TEST-secret-v1")` reduced mod the ristretto255 group
order via `crypto_core_ristretto255_scalar_reduce` — **test-only, never use
this or any other seed-derived value as the real production secret**):
normalize_exact / phonetic_key / lsh_band_tokens outputs for ~14
representative names, plus the resulting `H1` points and derived AES keys
for each. An implementation is conforming only if it reproduces every
vector exactly. The full 32-entry MinHash coefficient table is included at
the top of the same file (only the first 24 are used per the banding
scheme above).

### Out of scope for v1

- Verifiable OPRF / zero-knowledge proof of correct server exponentiation
  (would defend against a malicious, not just honest-but-curious, server —
  not needed for this threat model since the operator runs both sides).
- Server-side rate limiting beyond the simple per-user daily counter
  described above (no need for anything fancier like adaptive throttling
  at this scale).
- Cross-device manifest cache sync.
