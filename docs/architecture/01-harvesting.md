# 1 · Harvesting

> [↑ Hub](README.md) · *Generated from source on 2026-09-04.*

Data acquisition. Each source has a client (in `src/data/`) and, where appropriate, a
top-level `harvest_*.py` / `add_*.py` script that pulls records into the shared SQLite
store (`DBManager`).

```mermaid
flowchart TD
    subgraph CLIENTS["src/data/ clients"]
        IRS["irs_client.py"]
        SEC["sec_client.py"]
        F4["form4_client.py"]
        FD["form_d_client.py"]
        WIKI["wikidata_client.py"]
        EP["epstein_client.py"]
        LAW["law_firm_client.py"]
        PEN["pension_client.py"]
        POL["politician_client.py"]
        ADV["adv_client.py"]
        BIO["bio_parser.py"]
    end

    subgraph SCRIPTS["harvest / add scripts"]
        H1["harvest_level_a.py · harvest_irs_bulk.py<br/>harvest_grantee_boards.py"]
        H2["gdelt_gkg_harvester.py"]
        H3["harvest_money_managers.py · harvest_elite_300.py"]
        H4["batch_wikidata_enrich.py · add_cabinet_via_wikidata.py"]
        H5["add_evidence.py · add_ford_board.py · add_llc_officers.py"]
    end

    CLIENTS --> DB[("SQLite<br/>DBManager (pipeline_cache.db)")]
    SCRIPTS --> DB
    DB --> BUILD["graph build"]

    click BUILD "02-graph-building.md" "Graph building"
```

## Sources

| Source | Client / script | What it provides |
|---|---|---|
| IRS Form 990 | `irs_client.py`, `harvest_irs_bulk.py`, `harvest_level_a.py`, `harvest_grantee_boards.py` | foundations, grantees, boards |
| SEC | `sec_client.py`, `form4_client.py`, `form_d_client.py` | insider trades (Form 4), fund raises (Form D) |
| GDELT GKG | `gdelt_gkg_harvester.py` | news co-mentions for enrichment |
| Wikidata | `wikidata_client.py`, `batch_wikidata_enrich.py` | entity enrichment, cabinet memberships |
| Epstein documents | `epstein_client.py`, `add_evidence.py` | committee evidence records |
| Other | `law_firm_client.py`, `pension_client.py`, `politician_client.py`, `adv_client.py`, `bio_parser.py` | law-firm links, pensions, politicians, bio parsing |
| Death dates | `fetch_death_dates.py` | deceased records (`deceased.json`) |

## Storage

- **SQLite** via `src/data/db_manager.py` (`DBManager`) is the primary working store.
- Enrichment/identity maps live alongside it: `qid_map.jsonl`, `qid_overlap_edges.jsonl`,
  `enrichment_snapshot.csv`, `lp_sources_registry.csv`.
