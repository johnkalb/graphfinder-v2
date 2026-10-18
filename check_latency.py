"""Print the live site's response times per route (2026-10-18).

Reads /api/internal/user-links/latency (USER_LINKS_SYNC_TOKEN header, a
Cloudflare Bypass path like sync_user_links.py's), which summarises the
request_logs table the web app writes for every request.

  python check_latency.py            # last 24 hours
  python check_latency.py --hours 2  # just the last two hours
"""
import argparse
import os
import sys

import requests

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
import pg_secret  # noqa: E402

URL = os.environ.get("LATENCY_URL", "https://sixdegrees.net/api/internal/user-links/latency")
WATCH = ("/api/search", "/api/path", "/api/entity", "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args()
    r = requests.get(URL, params={"hours": args.hours},
                     headers={"X-Sync-Token": pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"),
                              "User-Agent": "sixdegrees-latency-check"}, timeout=60)
    r.raise_for_status()
    d = r.json()
    print(f"Last {d['hours']:g} h: {d['rows']:,} requests\n")
    print(f"{'route':42} {'n':>6} {'p50':>8} {'p90':>8} {'p99':>8} {'max':>8} {'>2s':>5}   (ms)")
    for x in d["routes"]:
        mark = " <" if x["route"] in WATCH else ""
        print(f"{x['route'][:42]:42} {x['n']:>6} {x['p50']:>8.0f} {x['p90']:>8.0f} {x['p99']:>8.0f} "
              f"{x['max']:>8.0f} {x['over_2s']:>5}{mark}")
    print("\nSlowest requests:")
    for x in d["slowest"][:15]:
        print(f"  {x['ms']:>7} ms  {x['status']}  {x['at']}  {x['route']}")


if __name__ == "__main__":
    main()
