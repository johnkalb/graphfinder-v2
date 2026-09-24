-- Keep harvest_cursors.updated_at current on every update.
--
-- Under SQLite, "INSERT OR REPLACE" deleted and re-inserted the row, so
-- updated_at picked up its DEFAULT again on every cursor save. pg_shim
-- translates that to "ON CONFLICT (source) DO UPDATE SET <listed columns>",
-- which leaves updated_at untouched -- so since the Postgres cutover every
-- cursor saved that way looked frozen, and pipeline_health_check.py flagged
-- healthy jobs as STALE (irs990_by_assets, gdelt_cooccurrence_daily; found
-- 2026-09-24). A trigger fixes it for every caller, including the pg_shim
-- copies deployed on optiplex.

CREATE OR REPLACE FUNCTION harvest_cursors_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_harvest_cursors_updated_at ON harvest_cursors;
CREATE TRIGGER trg_harvest_cursors_updated_at
    BEFORE UPDATE ON harvest_cursors
    FOR EACH ROW EXECUTE FUNCTION harvest_cursors_touch_updated_at();
