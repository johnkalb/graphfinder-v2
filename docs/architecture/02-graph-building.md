# 2 · Graph building

> [↑ Hub](README.md) · *Generated from source on 2026-09-04.*

Compiles the SQLite records into a deduplicated entity graph and prebuilds the artifacts
the web app serves.

```mermaid
flowchart TD
    DB[("SQLite")] --> COMPILE["graph_compiler.py<br/>GraphCompiler (networkx)<br/>clean_name · dedupe"]
    COMPILE --> GRAPH["entity graph<br/>(nodes + edges)"]

    GRAPH --> IDX["build_index.py<br/>graph pickle + dedup name index<br/>(filters location-type nodes)"]
    IDX --> ARTIFACTS["webapp/data/*.json.gz"]

    GRAPH --> CENT["build_centrality_extras.py<br/>centrality metrics"]
    GRAPH --> GROUP["build_group_rankings.py<br/>group rankings"]
    GRAPH --> CRAWLIE["build_crawlie_facts.py<br/>crawlie facts"]
    GRAPH --> NAMES["build_compact_names_scored.py<br/>compact names"]
    GRAPH --> SEARCH["build_search_from_scored.py<br/>search index"]
    GRAPH --> GEPHI["gephi_exporter.py<br/>Gephi export"]

    CENT --> ARTIFACTS
    GROUP --> ARTIFACTS
    CRAWLIE --> ARTIFACTS
    NAMES --> ARTIFACTS
    SEARCH --> ARTIFACTS
```

## Key artifacts (`webapp/data/`)

- `graph_edges.json.gz`, `graph_scored.json.gz` — the edge lists (scored in the next
  stage).
- `search_index.json.gz` — search index over deduplicated names.
- `centrality_extras.json.gz`, `group_rankings.json.gz` — precomputed metrics.
- `compact_names.json.gz`, `crawlie_facts.json.gz`, `evidence.json.gz`, `deceased.json`.

## `build_index.py`

The central build script: loads the graph from SQLite via `DBManager`, runs
`GraphCompiler.build_graph()`, and writes the graph pickle + a deduplicated name index,
**filtering out location-type nodes** (houses, addresses, phone numbers) from search.
