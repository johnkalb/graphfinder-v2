#!/usr/bin/env python3
"""Build the compressed contact PSI manifest from a JSON records file."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / "webapp"))
from contact_psi import manifest

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("records", help="JSON array of {id,name[,score]} records")
    parser.add_argument("output_dir", help="output directory for sharded manifest .json.gz files")
    parser.add_argument("--key-version", type=int, default=int(os.environ.get("CONTACT_PSI_KEY_VERSION", "1")))
    parser.add_argument("--shard-hex-chars", type=int, default=manifest.SHARD_HEX_CHARS,
                         help="prefix hex chars per shard (default 2 = 256 shards)")
    args = parser.parse_args()
    secret_b64 = os.environ.get("CONTACT_PSI_SERVER_SECRET")
    if not secret_b64:
        raise SystemExit("CONTACT_PSI_SERVER_SECRET is required")
    records = json.loads(Path(args.records).read_text())
    secret = base64.b64decode(secret_b64)
    built, entries = manifest.build_manifest(records, secret, args.key_version)
    shard_sizes = manifest.save_sharded_manifest(built, args.output_dir, args.shard_hex_chars)
    logging.basicConfig(level=logging.INFO)
    logging.info(
        "contact PSI manifest: entries=%d buckets=%d shards=%d total_compressed_bytes=%d max_shard_bytes=%d avg_shard_bytes=%.0f",
        entries, len(built["buckets"]), len(shard_sizes), sum(shard_sizes.values()),
        max(shard_sizes.values(), default=0), (sum(shard_sizes.values()) / len(shard_sizes)) if shard_sizes else 0,
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
