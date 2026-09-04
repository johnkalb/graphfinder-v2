# 5 · Ops & compliance

> [↑ Hub](README.md) · *Generated from source on 2026-09-04.*

Operational monitoring and legal/administrative safeguards for a controlled-access
investigation platform.

```mermaid
flowchart TD
    OPS["ops_monitor.py<br/>operational metrics"] --> METRICS[("ops_metrics.db")]
    LEGAL["legal_compliance.py"] --> COMP[("legal_compliance.db")]
    CHECK["check_irs_status.py ·<br/>check_org_board_coverage.py"] --> STATUS["coverage / status checks"]

    ADMIN["admin queue"] --> SUB[("user_submissions.db")]
```

## Components

- **`ops_monitor.py`** — operational metrics (`ops_metrics.db`).
- **`legal_compliance.py`** — compliance checks (`legal_compliance.db`).
- **`check_irs_status.py` / `check_org_board_coverage.py`** — coverage and status checks
  over the harvested data.
- **Admin queue** — user submissions with visible success/error feedback
  (`user_submissions.db`).

> Per the project's operating rule, development/admin surfaces are permanently excluded
> from analytics, and the purge tool has been removed. Code and deployment changes to this
> repository are owned by **Claude** — this document set is read-only advisory output.
