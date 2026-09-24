# Spec: GDELT Co-occurrence Classification

**Status:** Design validated via prototyping (real data, real models) — not yet implemented
**Created:** 2026-09-13
**Author:** Claude (design + prototyping)
**Origin:** Follow-on from the pipeline_cache SQLite→Postgres migration (see
`project_postgres_migration` memory). While auditing data completeness post-migration, found that
**7,277,000 distinct PERSON names sit in `relationships`, but only 949,725 come from real structured
sources** (SEC, FEC, IRS-990, LDA, ADV, AmLaw) — the rest is GDELT's own raw news-NER output
(`source_data='GDELT_FULL'`, 141,935,945 rows), currently contributing **zero** nodes or edges to
the live graph.

## Why GDELT co-occurrence is unused today

`MENTIONED_WITH` (GDELT's relation type) is dropped unconditionally in `build_scored_edges.py`'s
`DROP_RELATIONS` set (since 2026-08-24) — not "included only when co-occurring with a known person,"
as originally remembered, but never included at all. The reason: it was 84% of raw relationship
volume, its noise (GDELT's NER fabricates PERSON entities from boilerplate — "whatsapp linkedin,"
truncated names) created fake mega-hub nodes, and it bloated `graph_scored.json.gz` past GitHub's
100MB limit while tanking k-shortest-paths performance (31s worst case vs. 4.9s dropped). A
live, on-demand fallback exists (`webapp/mentioned_with_fallback.py`, Claude Haiku, triggered only
when the primary graph search returns zero paths) but classifies from article **title + URL only**,
never full text, and its real-world hit rate was unconfirmed as of 2026-08-25.

## What this session validated (prototyping, in order)

1. **CAMEO event-code join** (`gdelt_gkg_harvester.py`'s existing attempt at richer signal): dead
   end. Checked its 420,099 already-harvested rows — 100% fell through to the generic
   `MENTIONED_WITH` default. GDELT's coded events resolve to generic role/country tokens
   (`PRESIDENT`, `GOV`), disconnected from GKG's free-text-extracted named individuals.
2. **Tone + Themes only** (both already present, unused, in every GKG record downloaded by the
   existing harvesters — confirmed 100%/100%/90% field coverage on a live sample): real signal
   (a consistent criminal-case tone/theme cluster correctly flagged a real court case across 3
   outlets) but hits a hard ceiling — GDELT's theme tags are document-level, not attributed to a
   specific named entity. Can tell you "this pair is connected through something adversarial," not
   "who did what to whom."
3. **REBEL (`Babelscape/rebel-large`) with real article-text fetch**: works, with two real caveats
   found via testing, not assumption:
   - **512-token truncation loses real facts** in longer articles (confirmed: two real relations —
     `Home Depot founded by Arthur Blank`, an athlete's event participation — sat past the
     truncation cutoff and were invisible until chunking was added).
   - **Same-document self-consistency (multi-beam resampling) does NOT catch hallucinations.** A
     confirmed factual error (`John Swinney member of Scottish Labour` — he leads a different
     party) scored 10/10 beam votes, higher than the true fact (1/10). Resampling the same text
     catches random noise, not a systematic misread the model repeats every time.
4. **Chunking (token-aware sliding window) fixed the truncation problem** — confirmed recovering
   both previously-invisible facts once implemented.
5. **Cross-document corroboration (2+ independent, non-duplicate articles) is the confidence signal
   that actually works.** Validated on two real pairs:
   - Meghan Markle / Prince Harry (a real, known relationship): 6 of 7 independently-written
     articles converged on `spouse`. Isolated wrong triplets (`spouse: Prince William`,
     `mother: Princess Anne`) each appeared in exactly one document and never recurred.
   - Matthew McGowan / Alex Turner (a pair with genuinely no direct relationship — both are
     incidentally connected to a third person, John Setka, who isn't in our known-names pool):
     5 candidate URLs collapsed to **1** independent source after near-duplicate detection (they
     were one Australian Associated Press wire story republished verbatim across 5 regional
     sites) — correctly below any reasonable corroboration threshold, correctly not promoted.
6. **Near-duplicate detection is required**, not optional — without it, one wire story syndicated
   across N sites looks like "N independent sources agree," which is false and would systematically
   let syndicated content out-vote genuine independent reporting. Validated threshold: text
   similarity ≥0.7 cleanly separates real duplicates (0.99–1.00 observed) from independent articles
   (<0.5 observed) — wide safety margin, simple `difflib` ratio sufficient at this scale.

**Working prototype** (`corroboration_pipeline.py`, session scratchpad — not yet in the repo):
dedup → chunked REBEL per independent document → promote a relation only if **≥2 independent
sources agree** (user-set threshold). Validated correct on both cases above.

## Real measured costs

- REBEL inference (CPU, this machine, 6-beam decode): **11.6s/chunk**, measured.
- Article fetch (`trafilatura`): **~1s/doc**, ~83-85% success rate observed (paywalls/dead links
  account for the rest).
- Per pair needing 2+ confirmed sources: **~1-1.5 min** (REBEL dominates, not network).
- Clearing the full 141.9M-pair backlog: **not feasible** — the existing `relationships` unique
  constraint already deduped away the multi-article evidence corroboration needs (confirmed
  directly: re-inserting a known duplicate triple was correctly blocked — this is NOT a dedup bug,
  it's the reason the backlog can't be corroborated without fresh queries). At ~60-90s/pair,
  full-backlog processing is ~270 CPU-years.

## Three separable efforts

### Effort 1 — Daily incremental classification (forward-looking, cheapest, do this)

Check newly-added people (from LDA/IRS-990/ADV/AmLaw harvests) against GDELT co-occurrence data
each day, using the validated corroboration pipeline. Estimated volume: tens to a couple hundred
new confirmed people/day, a fraction of which have any GDELT presence at all → **roughly 5-60
pairs/day → 5-100 minutes of compute/day**, trivially a nightly batch job on existing hardware.
This is the original framing from before the deep-dive and remains the right shape for ongoing
operation.

**Dependencies:** the corroboration pipeline (validated, not yet productionized), a way to detect
"newly added person" (harvest_cursors timestamps or a simple daily diff), GDELT DOC API access
(already used by `mentioned_with_fallback.py`) or GKG-file scanning for candidate articles.

### Effort 2 — Stop discarding future evidence (schema change, low risk, enables everything else)

The harvester's per-pair dedup (`ON CONFLICT DO NOTHING`) is correct and working as designed
(verified directly this session) but throws away exactly the multi-sighting evidence corroboration
needs. Fix: a new side table, not a change to `relationships`' existing constraint (that constraint
is load-bearing for every other harvester and just came through a careful Postgres migration):

```sql
CREATE TABLE gdelt_cooccurrence_evidence (
    pair_hash    TEXT PRIMARY KEY,      -- md5(sorted lowercase name pair)
    name_a       TEXT,
    name_b       TEXT,
    occurrence_count INT DEFAULT 1,      -- true count, uncapped
    sample_urls  TEXT[],                 -- up to N distinct URLs, capped (proposed N=5)
    first_seen   TIMESTAMPTZ,
    last_seen    TIMESTAMPTZ,
    promotion_status TEXT DEFAULT 'unreviewed'
);
```

Harvester logic changes from "skip if pair exists" to "always increment count; append to
`sample_urls` only while under the cap." Bounded storage growth per pair regardless of how famous
either person is (a pair mentioned 500 times only ever stores 5 sample URLs).

**This mostly pays for itself**: the harvester's backward sweep has only covered ~14% of the
planned 2015-2026 range (currently at ~Feb 2025) — shipping this now means the **remaining ~86% of
the archive accumulates real evidence for free**, as a byproduct of a sweep that's going to happen
anyway. Only the already-swept 14% (today back to Feb 2025) would need a dedicated backfill re-scan
to recover its evidence retroactively (~285GB of re-download for that window specifically, GKG-file
counting only, no REBEL needed — cheap relative to the classification cost).

**Dependencies:** none beyond the already-completed Postgres migration. No risk to existing
harvester correctness since `relationships`' write path is untouched.

### Effort 3 — Targeted historical mining (bounded, high-value slice of the existing backlog)

Rather than attempting the full 141.9M-pair backlog (infeasible) or even the "appears 2-3 times"
slice sized against the whole archive (~16M pairs estimated, ~35-40 CPU-years — still too large),
target new people who co-occur with **already-important existing graph nodes** specifically — the
highest-value place to spend classification budget, since it directly grows the graph outward from
established hubs rather than processing the long tail indiscriminately.

**Measured, not estimated** (computed directly against the live 141.9M-row table):

| Population | Count |
|---|---|
| New candidates co-occurring with the top-1000 most-connected existing people | 609,933 |
| ...of which, from Donald Trump alone | 414,097 (68%) |
| New candidates, **excluding Trump** | 357,089 |
| ...co-occurring with exactly 2-3 *different* top-1000 hubs | 77,977 |
| ...co-occurring with 2+ different top-1000 hubs | 113,161 |

Trump's share is disproportionate and low-value (a name appearing once near Trump in a crowd/quote
list is exactly the low-signal mention this whole project exists to filter out, not chase) —
excluding him is a deliberate choice, not an oversight. The long tail also contains a distinct noise
class worth handling separately: some candidates co-occur with 50-96 *different* top-1000 hubs —
almost certainly journalists/anchors/wire reporters covering everyone, not people with 96 genuine
relationships. High hub-count is not automatically high value here.

**Estimated cost**: the 77,977-candidate (2-3 hub) population needs ~2.5 hub-pairs classified each
on average → ~0.45 CPU-years, tractable in a couple of weeks with modest parallelization.

**Dependencies:** the corroboration pipeline (Effort 1/2's shared infrastructure), a decision on the
exact hub-count band (2-3 vs. 2+ vs. something narrower to exclude the media/journalist tail), and
whether "2-3 hub co-occurrence" is treated as sufficient corroboration on its own or still requires
the 2-independent-article check per individual hub-pair.

## Shared infrastructure (built once, used by all three efforts)

- **Chunked REBEL relation extraction** — token-aware sliding window, not truncation. Validated.
- **Near-duplicate detection** — text similarity ≥0.7 → collapse to one source. Validated.
- **Cross-document corroboration threshold** — promote only when ≥2 independent sources agree
  (user-set; validated against one true-positive and one true-negative real case).
- **Article text fetch** — `trafilatura`, ~1s/doc, ~83-85% success rate.

None of this exists in the repo yet — it's session-scratchpad prototype code
(`corroboration_pipeline.py` and related scripts). Productionizing means: packaging as a real
module (likely under `src/data/` or a new `gdelt_classification/` package), deciding where model
weights/inference run (this machine's CPU was used for all prototyping — ~11.6s/chunk; worth
checking whether GPU acceleration is available anywhere in the existing Tailscale fleet, e.g. the
`docling_parser` GCP VM, before committing to a throughput plan), and wiring into the existing
harvest-cursor/Task-Scheduler conventions the rest of the pipeline uses.

## Open decisions (not yet made)

1. Alias/entity resolution — "Duchess of Sussex" wasn't recognized as "Meghan Markle" during
   corroboration counting, meaning the current design *undercounts* real agreement (the safe
   direction to be wrong in, but real lost signal). Not yet designed.
2. Exact hub-count band for Effort 3 (2-3? 2+? an upper bound to exclude the journalist-tail noise?).
3. Whether Effort 3's multi-hub signal is sufficient on its own, or whether each hub-pair still
   needs its own independent 2-source corroboration check on top of it.
4. Where Effort 3's output writes to — presumably it feeds the same `gdelt_cooccurrence_evidence` /
   promotion mechanism as Effort 1, once a candidate clears the corroboration bar, using a relation
   type `build_scored_edges.py` doesn't drop (or a new rule there keeping `MENTIONED_WITH`
   specifically when corroborated).
5. Compute placement and parallelization plan for Effort 3 specifically (single-machine CPU vs.
   distributing across the existing fleet vs. GPU).

## Related memory

- `project_postgres_migration` — the migration that surfaced this dataset.
- `project_data_completeness_2026_09_12` — the data-completeness audit this grew out of.
- `project_pagerank_growth_features` — origin of the Epstein-vs-senators PageRank finding that
  first motivated looking at GDELT data more closely.
