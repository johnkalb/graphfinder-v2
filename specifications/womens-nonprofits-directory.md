# Spec: Sector-Filtered Nonprofit Directory Pipeline (Phase 1: Women's Issues)

**Status:** Draft for review → Claude tests → implement
**Created:** 2026-08-21
**Origin:** Gemini GreenCollab artifact (hallucinated delivery) + irs_990 org_missions capability. New use case: sector directories from IRS 990 infrastructure.

## Goal

A deterministic pipeline producing a verified SQLite directory of 501(c)(3) organizations in a target sector (Phase 1: women's issues, excluding voting/electoral orgs), with real financials and real mission statements. Generalizable to other sectors later.

## Data Sources (all authoritative)

1. **IRS EO Business Master File (BMF)** — bulk CSV: EIN, legal name, city, state, NTEE code, subsection. Download: https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf (or the data.gov mirror).
2. **ProPublica Nonprofit Explorer API** — per-EIN: total_revenue, program_expenses (functional expenses), tax_year. Rate-limited; cache results.
3. **org_missions (sixdegrees pipeline, Optiplex)** — real mission statements keyed by EIN from 990 XMLs. LEFT JOIN — many orgs will have NULL mission until the harvester catches up; that's acceptable and documented.

## Filter (deterministic, unit-tested)

**Include** (NTEE major codes, exact match on prefix):
- P46 (DV shelters), P43 (family violence), E42 (reproductive health), E22 (women's health NEC), R24 (women's rights advocacy), U30 (women in STEM/physical sciences), B40 (women in higher ed/professions), S31 (vocational for women), O54 (girls' youth development), L20 (women's housing), W30 (microfinance incl. women's), I21 (anti-trafficking), P44 (permanent supportive housing, women)

**Exclude** (hard reject):
- NTEE R40, R60-series (voter education/registration/civil rights-voting)
- Name regex: `(?i)\b(vote|voter|voting|election|ballot|electoral|league of women voters)\b`

## Output

`data/womens_issues_nonprofits.db` (checked into repo? no — generated artifact, gitignored; reproducible from pipeline):

```sql
CREATE TABLE womens_501c3_nonprofits (
  id INTEGER PK, ein TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  city TEXT, state TEXT, ntee_code TEXT, ntee_category TEXT,
  subsection TEXT DEFAULT '501(c)(3)',
  mission TEXT,                    -- real from org_missions; NULL if not yet harvested
  primary_focus TEXT,              -- derived from NTEE code
  website TEXT,                    -- NULL phase 1
  total_revenue REAL, program_expenses REAL, tax_year INTEGER,
  efficiency_ratio REAL,           -- program_expenses / total_revenue (NULL-safe)
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE VIRTUAL TABLE womens_nonprofits_fts USING fts5(ein UNINDEXED, name, mission, primary_focus, content='womens_501c3_nonprofits', content_rowid='id');
```

Plus `scripts/query_womens_db.py` CLI (search/ein/state/json-export).

## Pipeline Steps (Dagster asset in sixdegrees)

1. `download_bmf` — fetch EO BMF extract (cache in data/raw/, date-stamped)
2. `filter_sector` — NTEE include/exclude + name regex → candidate set (pure function, testable)
3. `enrich_financials` — ProPublica API per EIN (cached, resumable)
4. `join_missions` — LEFT JOIN org_missions from pipeline_cache.db (Optiplex sync'd copy)
5. `build_directory_db` — write SQLite + FTS5 + validation report (counts by NTEE, % with mission, % with financials)

## Validation

- All 31 Gemini seed EINs that are genuinely women's-issues must appear in output (whitelist check — validates filter recall)
- Zero orgs with voting-related NTEE/name in output
- Row count sanity: expect 5K–30K orgs nationally (report actual)
- Spot-check 10 random EINs against IRS TEOS public search

## Anti-goals (Phase 1)

- No LLM-generated mission text — NULLs allowed, filled by irs990 harvest over time
- No website scraping for org sites (phase 2)
- No web UI (GreenCollab React app is reference only; maybe phase 3)

## TDD Plan

Claude writes tests first from this spec:
- filter: include/exclude NTEE + name regex table tests (incl. edge cases like "League of Women Voters" → excluded despite "women" in name)
- schema: DB builds, FTS5 works, EIN uniqueness enforced
- validation: the 31-seed whitelist test
- efficiency_ratio NULL handling (division by zero)
