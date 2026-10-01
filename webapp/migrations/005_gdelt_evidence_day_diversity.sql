-- Spread each pair's sample article URLs across different news days.
--
-- gdelt_full_harvester.py used to keep the FIRST 5 article URLs per pair, so
-- a pair's evidence was usually one news day -- often one syndicated wire
-- story on 5 sites -- which rarely states how the two people are related
-- (diagnosed 2026-09-29: Trump/Michael Cohen, Biden/Blinken). Now each URL
-- carries its GKG file day, at most one URL per day is kept, up to 8 per
-- pair, and once full a new day replaces the most crowded stored day only if
-- that widens the spread. URLs stored before this migration have no day
-- (NULL) and are phased out first, keeping two of them.

ALTER TABLE gdelt_cooccurrence_evidence
    ADD COLUMN IF NOT EXISTS sample_days DATE[] NOT NULL DEFAULT '{}';

CREATE OR REPLACE FUNCTION merge_sample_urls(old_urls TEXT[], old_days DATE[], new_url TEXT,
                                             new_day DATE, max_n INT)
RETURNS TABLE(urls TEXT[], days DATE[]) LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE
    u TEXT[] := COALESCE(old_urls, '{}');
    d DATE[] := COALESCE(old_days, '{}');
    n INT;
    i INT; j INT;
    n_undated INT := 0;
    best_i INT; best_gap INT; gap INT; new_gap INT;
BEGIN
    n := cardinality(u);
    WHILE cardinality(d) < n LOOP          -- pre-migration URLs: day unknown
        d := d || NULL::DATE;
    END LOOP;
    IF new_url IS NULL OR new_url = '' OR new_url = ANY(u)
       OR (new_day IS NOT NULL AND new_day = ANY(d)) THEN
        RETURN QUERY SELECT u, d; RETURN;
    END IF;
    IF n < max_n THEN
        RETURN QUERY SELECT u || new_url, d || new_day; RETURN;
    END IF;
    -- Full. Phase out undated pre-migration URLs first, keeping two.
    FOR i IN 1..n LOOP
        IF d[i] IS NULL THEN n_undated := n_undated + 1; best_i := i; END IF;
    END LOOP;
    IF n_undated > 2 THEN
        u[best_i] := new_url; d[best_i] := new_day;
        RETURN QUERY SELECT u, d; RETURN;
    END IF;
    -- Otherwise replace the dated entry closest to another dated entry, but
    -- only if the new day would sit farther from its nearest neighbour.
    best_i := NULL;
    FOR i IN 1..n LOOP
        CONTINUE WHEN d[i] IS NULL;
        gap := NULL;
        FOR j IN 1..n LOOP
            CONTINUE WHEN j = i OR d[j] IS NULL;
            IF gap IS NULL OR abs(d[i] - d[j]) < gap THEN gap := abs(d[i] - d[j]); END IF;
        END LOOP;
        IF gap IS NOT NULL AND (best_gap IS NULL OR gap < best_gap) THEN
            best_gap := gap; best_i := i;
        END IF;
    END LOOP;
    IF best_i IS NULL OR new_day IS NULL THEN
        RETURN QUERY SELECT u, d; RETURN;
    END IF;
    new_gap := NULL;
    FOR j IN 1..n LOOP
        CONTINUE WHEN j = best_i OR d[j] IS NULL;
        IF new_gap IS NULL OR abs(new_day - d[j]) < new_gap THEN new_gap := abs(new_day - d[j]); END IF;
    END LOOP;
    IF new_gap IS NOT NULL AND new_gap > best_gap THEN
        u[best_i] := new_url; d[best_i] := new_day;
    END IF;
    RETURN QUERY SELECT u, d;
END;
$$;
