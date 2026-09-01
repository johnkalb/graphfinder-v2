"""Audit relation_type hygiene across the whole harvester fleet.

Formalizes the manual detective work that found two real, previously-invisible
bugs this session (2026-08-29/31):
  - FEC: embedded the dollar amount into relation_type ("DONATION ($250)"),
    breaking categorize()'s exact-match logic for ~2M rows.
  - IRS_990/IRS_990_TEOS: wrote non-normalized "POSITION (<role>)" text with
    mixed case/abbreviations/truncation across ~65K rows, several variants of
    which either fall through to OTHER or false-match into the wrong category
    via a substring collision (e.g. "DIRECTOR" contains "CTO").

Both bugs were only found because a *new* feature happened to need to
interpret relation_type cleanly. No harvester in this pipeline validates
relation_type shape at write time (confirmed: zero Pydantic/BaseModel usage
across all ~42 harvester agents) -- this script is the cheap, read-only,
zero-regression-risk first line of defense: run it periodically and it
surfaces this exact bug class before a new feature stumbles onto it months
later.

Flags, per source_data:
  - total rows and distinct relation_type count (a source with dozens/hundreds
    of distinct values for what should be a small fixed vocabulary is the
    "POSITION (<role>)"-style red flag)
  - row share landing in categorize()'s OTHER bucket (silently uncategorized)
  - individual relation_type strings that look like they have variable data
    baked in (digits, or a parenthesized free-text suffix) rather than being
    a clean constant

Read-only. Streams the cursor (never .fetchall()) -- same reasoning as
build_scored_edges.py: the relationships table is 100M+ rows, materializing
results holds multiple GB and dominates runtime.

Usage: python audit_relation_types.py [--min-rows N] [--top N]
"""
import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "webapp"))
from relation_categories import categorize, validate_relation_type

DB_PATH = os.environ.get("DB_PATH", "C:/Users/johnk/data/pipeline_cache.db")


def suspicious_reasons(relation_type):
    """Thin wrapper around the shared validate_relation_type() check (see
    relation_categories.py) -- same shape-check harvesters are expected to
    call at write time, applied here retroactively across already-harvested
    data."""
    is_clean, reasons = validate_relation_type(relation_type)
    return [] if is_clean else reasons


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rows", type=int, default=100,
                     help="only report source_data groups with at least this many total rows")
    ap.add_argument("--top", type=int, default=10,
                     help="how many suspicious relation_type values to show per source")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.arraysize = 10000

    # source_data -> relation_type -> row count
    counts = defaultdict(Counter)
    n_scanned = 0

    print("Scanning relationships (source_data, relation_type)...", flush=True)
    cur.execute("SELECT source_data, relation_type FROM relationships")
    for source_data, relation_type in cur:
        n_scanned += 1
        if n_scanned % 10_000_000 == 0:
            print(f"  ...scanned {n_scanned:,} rows", flush=True)
        counts[source_data or "(null)"][relation_type or "(null)"] += 1
    conn.close()
    print(f"Scanned {n_scanned:,} rows across {len(counts)} source_data tags.\n", flush=True)

    report_rows = []
    for source_data, rt_counts in counts.items():
        total = sum(rt_counts.values())
        if total < args.min_rows:
            continue
        other_rows = sum(c for rt, c in rt_counts.items() if categorize(rt) == "OTHER")
        other_pct = 100.0 * other_rows / total
        suspicious = []
        for rt, c in rt_counts.items():
            reasons = suspicious_reasons(rt)
            if reasons:
                suspicious.append((c, rt, reasons))
        suspicious.sort(reverse=True)
        report_rows.append((source_data, total, len(rt_counts), other_rows, other_pct, suspicious))

    # Worst offenders (by suspicious-row volume, then OTHER-bucket share) first.
    report_rows.sort(key=lambda r: (sum(c for c, _, _ in r[5]), r[4]), reverse=True)

    print("=" * 100)
    print(f"{'source_data':<24} {'total_rows':>12} {'distinct_rt':>12} {'other_rows':>12} {'other_%':>8}")
    print("=" * 100)
    for source_data, total, n_distinct, other_rows, other_pct, suspicious in report_rows:
        flag = " <-- HIGH DISTINCT RT COUNT" if n_distinct > 30 else ""
        print(f"{source_data:<24} {total:>12,} {n_distinct:>12,} {other_rows:>12,} {other_pct:>7.1f}%{flag}")

    print("\n" + "=" * 100)
    print("Suspicious relation_type values (digits or parenthesized free text) by source_data")
    print("=" * 100)
    for source_data, total, n_distinct, other_rows, other_pct, suspicious in report_rows:
        if not suspicious:
            continue
        print(f"\n{source_data} ({sum(c for c, _, _ in suspicious):,} suspicious rows across "
              f"{len(suspicious)} distinct values):")
        for c, rt, reasons in suspicious[:args.top]:
            cat = categorize(rt)
            print(f"    {c:>10,}  {rt!r:<40} -> categorize()={cat:<20} ({', '.join(reasons)})")
        if len(suspicious) > args.top:
            print(f"    ... and {len(suspicious) - args.top} more distinct suspicious values")


if __name__ == "__main__":
    main()
