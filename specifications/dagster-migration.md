# Spec: sixdegrees harvest pipeline → Dagster migration

**Status:** Draft for review → Claude tests → implement (HELD on DB-corruption repair)
**Created:** 2026-09-12
**Author:** Hermes (control point — spec + verify)
**Origin:** Coordination review. The harvest pipeline (~31 cron agents → SQLite `pipeline_cache.db`
with a `harvest_cursors` table) has no per-source telemetry, which let a harvester fail silently
since July and left coverage gaps (e.g. 3% education history) undiscovered. Dagster provides the
orchestration + freshness/telemetry layer. This spec is the brief Claude implements test-first
after the current corruption repair completes.

## Goal

Migrate the sixdegrees harvest pipeline from Hermes-cron + cursor-driven agent scripts to
Dagster-orchestrated assets, **without changing extraction/parsing logic or the graph-serving
deployment**. Gain:

1. **Per-source heartbeat** = Dagster asset materialization timestamp + `harvest_cursors.updated_at`
   (the "silent-July" detector).
2. Schedules, retries, and failure alerting in one place (replaces `smart_runner.py` + the
   `cron-error-monitor` job).
3. A promotion path: exploratory script → tested asset → scheduled production asset.

## Hard constraints

- **Only Claude makes code/deploy changes to sixdegrees.** Hermes writes this spec and verifies
  (re-runs tests, checks freshness). No pipeline code changes by Hermes.
- **Do NOT touch graph serving** — the i-graph response-time constraint and the "no monolithic
  graph upload" rule stand; this spec is harvest-side only.
- **Extraction logic is reused, not rewritten.** Only the scheduling/state/orchestration layer
  changes. A migrated source must produce the same rows as its legacy script (regression-tested).
- **ToS compliance registry** unchanged (9 registered sources; weekly `legal-tos-check` continues).
- **TDD gate**: tests before implementation, green on WSL (and Windows where a fetch stays Windows-side).
- **Prerequisite**: a healthy `pipeline_cache.db` (the current corruption repair must complete first).

## Current architecture (as-built)

- 47 wrapper scripts under `AppData/Local/hermes/scripts/*_wrapper.py`, driven by Hermes cron
  (`no_agent=True`), each wrapping an agent Python script (`.hermes/agents/<name>/scripts/*.py`
  or a `Desktop/sixdegrees/*.py`).
- All agents write `relationships` rows into SQLite `pipeline_cache.db`, tracking progress via
  `harvest_cursors` (source PK → cursor_value, status, updated_at).
- `smart_runner.py` provides error detection + antigravity dispatch + one retry; a separate
  `cron-error-monitor` (30m) and `pipeline-status-report` (12h) watch the cron surface.
- Runs on Windows (Hermes cron + Windows venv) with SSL workarounds for `.gov` sites.
- Heavy sources (FEC 500 MB zips, IRS 990 monthly zips) exceed the 30-min wrapper timeout; some
  run on the Optiplex remote node.

## Target architecture

- One Dagster **asset per source**, tagged `compute_kind` (sparql / wikipedia / rest_api / scrape / python / report).
- One `ScheduleDefinition` per source (cron → schedule; same cadence).
- **Incremental state** stays in `harvest_cursors` (read/write inside the asset) — proven pattern,
  no need to force Dagster partitions initially. Partitions are an optional later refinement for heavy sources.
- **Retry** = Dagster `RetryPolicy` (replaces `smart_runner` retry). **Failure dispatch** = a Dagster
  sensor or `run_failure` hook; antigravity dispatch retained or dropped per-source (Phase 4 decision).
- **Data store stays SQLite** (`pipeline_cache.db`). Dagster's own run/event metadata uses a separate
  store (a local SQLite file; Postgres only if concurrency forces it).
- **Platform**: Dagster runs on WSL (pydantic-core requires Linux). Fetch scripts may stay Windows-side
  via the proven "Windows prefetch → shared SQLite → WSL materialize" boundary, or move into WSL where
  internet/TLS allow. Decision per source (see per-source table).

## Key decisions

1. **Incremental, source-by-source — not big-bang.** Each source is fully cut over (spec → tests →
   asset → schedule → verify freshness) before the next. A half-migrated state is the main schedule
   risk; per-source cutover avoids it.
2. **Phase 1 proves the pattern on tier-A simple scrapers first**, then rate-limited APIs, then heavy jobs last.
3. **Heartbeat is the acceptance signal** — a migrated source is "done" only when its freshness is
   observable and a stale source produces a detectable alert.
4. **smart_runner/antigravity retirement** is Phase 4 (cross-cutting), not a per-source blocker.

## Per-source mapping (tier assignment)

| Tier | Sources (representative) | Asset type | Notes |
|---|---|---|---|
| **A — simple scraper/roster** | federal depts (treasury, defense, justice, interior, agriculture, commerce, labor, hhs, hud, transportation, energy, education, va, dhs), military (army/navy/airforce/marines), 50-states, state legislature, congress legislators, religious leaders, state department | scrape / wikipedia / rest_api; 1 item or small batch per tick | Most numerous; the template-setter. |
| **B — rate-limited API poller** | SEC Form 4, GDELT, ADV/IAPD, LP-pension, IRS 990, FEC, lobbying, GitHub orgs, LittleSis, Wikidata legal, RECAP CourtListener, patent | rest_api / sparql; cursor/pagination | Rate limits + cursor logic already exist; wrap as assets. |
| **C — heavy / long-running** | FEC 500 MB zips, IRS 990 monthly zips, GDELT full archive | partitioned asset / backfill; remote or WSL | Need partitioning/backfill design; migrate last. |
| **M — monitoring/reconciliation** | person-reconciliation, pipeline-status-report, cron-error-monitor, legal-tos-check | python / report | `pipeline-status-report` is superseded by Dagster's own UI + the source-health asset. |

(Full inventory — 31+ sources — is enumerated during Phase 1 from `AppData/Local/hermes/scripts/*_wrapper.py`
and the cron list; this table fixes the tiering rule, not the roster.)

## Heartbeat / telemetry (the driver)

- Per source, freshness = max(`harvest_cursors.updated_at`) and the asset's last materialization time.
- A `source_health` report asset (compute_kind=report, reads the DB only) emits per-source freshness and
  flags staleness > per-source threshold.
- This feeds the coordination control board (`coordinator/reconcile.py`), replacing the "probe" that is
  currently on hold pending the DB repair.

## Phases

- **Phase 0 — Platform.** Dagster scaffold on WSL (`dagster_project/definitions.py`, `pyproject.toml`),
  SQLite sync boundary (Windows ↔ WSL), CI-style test runner. TDD: platform schema + resource tests.
- **Phase 1 — Tier A (template).** Migrate 3–5 simple scrapers fully; establish the asset + schedule +
  freshness pattern. Acceptance: migrated source produces identical rows (regression) + freshness observable.
- **Phase 2 — Tier B.** Rate-limited API pollers, source by source.
- **Phase 3 — Tier C.** Heavy jobs with partitioning/backfill.
- **Phase 4 — Cross-cutting.** Retire smart_runner; failure sensor/alert; source_health asset; wire to control board.

## Acceptance criteria

- ≥ 3 tier-A sources running as Dagster assets with schedules, tests green on WSL.
- Each migrated source shows freshness; a manually-staled source triggers a detectable alert.
- Regression: migrated source output == legacy script output on a fixed fixture (row-for-row or key-equivalent).
- Graph-serving deployment untouched (git diff shows no changes under the serving/deploy path).

## Risks

- **Windows/WSL split** (TLS/SSL, internet) is the top risk — resolved per-source with the prefetch boundary.
- **SQLite concurrency** under Dagster (multiple assets materializing) — reuse the `timeout=60` +
  `safe_commit` pattern; Postgres only if it bites.
- **Heavy jobs** don't fit "materialize an asset" — Phase 3 partitioning/backfill.
- **Duration** ~5–8 weeks agent-time; mitigated by source-by-source cutover (each increment independently shippable).

## Out of scope

- Graph-serving / i-graph changes (forbidden).
- New data sources (migration, not expansion).
- Model training / enrichment logic changes (extraction is reused).

## TDD plan (Claude writes tests first from this spec)

- Per migrated source: a contract test asserting the asset's output equals the legacy script's output on
  a shared fixture (and that the asset reads/writes `harvest_cursors` correctly).
- `source_health`: freshness computation + staleness-flag test on synthetic cursor timestamps.
- Schedule definitions: assert the cron mapping.
- Anti-regression: assert extraction functions are unchanged (import the legacy parser, don't reimplement).
