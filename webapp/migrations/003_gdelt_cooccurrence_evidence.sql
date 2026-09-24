-- Capped multi-sighting evidence for GDELT person co-occurrences, see
-- specifications/gdelt-cooccurrence-classification.md ("Effort 2"). The
-- relationships table's unique constraint correctly dedupes a pair to one
-- row (verified directly, not a bug) -- but that means every re-sighting of
-- an already-seen pair is silently discarded, which is exactly the
-- multi-article evidence that cross-document corroboration (the validated
-- confidence signal for relation classification) needs. This table stores
-- that evidence going forward, with storage bounded regardless of how often
-- a pair is mentioned (a pair seen 500 times still only stores 5 URLs).
--
-- Written by gdelt_full_harvester.py as a purely additive step alongside its
-- existing (unchanged) relationships insert -- see the harvester's own
-- comments at the call site.
--
-- Run once against the target database:
--   psql "$DATABASE_URL" -f webapp/migrations/003_gdelt_cooccurrence_evidence.sql

CREATE TABLE IF NOT EXISTS gdelt_cooccurrence_evidence (
    pair_hash        TEXT PRIMARY KEY,        -- md5("name_a|name_b"), both lowercased, alphabetically ordered
    name_a           TEXT NOT NULL,           -- original casing, alphabetically-lower of the pair
    name_b           TEXT NOT NULL,           -- original casing, alphabetically-higher of the pair
    occurrence_count INT NOT NULL DEFAULT 1,  -- true count, uncapped
    sample_urls      TEXT[] NOT NULL DEFAULT '{}',  -- distinct article URLs, capped at 5
    first_seen       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen        TIMESTAMPTZ NOT NULL DEFAULT now(),
    promotion_status TEXT NOT NULL DEFAULT 'unreviewed'  -- unreviewed | promoted | rejected
);

CREATE INDEX IF NOT EXISTS idx_gdelt_evidence_status_count
    ON gdelt_cooccurrence_evidence (promotion_status, occurrence_count DESC);
