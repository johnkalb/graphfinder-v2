#!/usr/bin/env python3
"""Build the contact-matching manifest (compact format 2, 2026-11-15).

  python build_contact_psi_manifest.py --from-graph [OUTPUT_DIR]
      every person in the scored graph (graph_scored.json.gz + person_nodes.json.gz
      from the nightly build), ranked by degree so the best-known namesakes
      come first in a bundle
  python build_contact_psi_manifest.py RECORDS.json [OUTPUT_DIR]
      a JSON array of {id, name[, score]}

Writes contact_psi_exact.bin (downloaded whole by every browser) and the 256
contact_psi_fuzzy_XX.bin shards into OUTPUT_DIR (default
webapp/data/contact_psi_manifest), replacing the old JSON shards. Needs
CONTACT_PSI_SERVER_SECRET (base64) -- the same secret the app has -- and
CONTACT_PSI_KEY_VERSION. Takes a few hours (one Ristretto evaluation per
name variant).
"""
from __future__ import annotations
import argparse
import base64
import gzip
import json
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "webapp"))
from contact_psi import manifest  # noqa: E402

GRAPH = Path(os.environ.get("PSI_GRAPH_DIR", r"C:\Users\johnk\graphfinder-clean\webapp\data"))


def graph_records():
    with gzip.open(GRAPH / "graph_scored.json.gz", "rt", encoding="utf-8") as f:
        g = json.load(f)
    with gzip.open(GRAPH / "person_nodes.json.gz", "rt", encoding="utf-8") as f:
        people = set(json.load(f))
    deg = Counter()
    for e in g["edges"]:
        deg[e[0]] += 1
        deg[e[1]] += 1
    nodes = g["nodes"]
    return [{"id": i, "name": nodes[i], "score": deg[i]} for i in sorted(people)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("records", nargs="?", help="JSON array of {id,name[,score]} (omit with --from-graph)")
    ap.add_argument("output_dir", nargs="?", default=str(HERE / "webapp" / "data" / "contact_psi_manifest"))
    ap.add_argument("--from-graph", action="store_true")
    ap.add_argument("--key-version", type=int, default=int(os.environ.get("CONTACT_PSI_KEY_VERSION", "1")))
    args = ap.parse_args()
    if args.from_graph and args.records and not args.records.endswith(".json"):
        args.output_dir, args.records = args.records, None
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    secret_b64 = os.environ.get("CONTACT_PSI_SERVER_SECRET")
    if not secret_b64:
        raise SystemExit("CONTACT_PSI_SERVER_SECRET is required")
    secret = base64.b64decode(secret_b64)
    t0 = time.time()
    records = graph_records() if args.from_graph else json.loads(Path(args.records).read_text(encoding="utf-8"))
    logging.info("records: %d", len(records))
    exact, fuzzy, stats = manifest.build_compact(
        records, secret, args.key_version,
        progress=lambda i, n: logging.info("evaluated %d / %d (%.0f min)", i, n, (time.time() - t0) / 60))
    manifest.save_compact(args.output_dir, exact, fuzzy)
    logging.info("contact PSI manifest (format 2, key_version %d): %s, %d fuzzy shards, %.0f min",
                 args.key_version, stats, len(fuzzy), (time.time() - t0) / 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
