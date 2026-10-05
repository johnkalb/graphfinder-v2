"""Pull links approved in the /admin review queue into the harvest database (2026-10-05).

"Add Me" contacts, LinkedIn connections and suggested links are approved on the
live site, which stores them in its own database (user_links). This fetches
them from /api/internal/user-links (USER_LINKS_SYNC_TOKEN header; the path is
a Cloudflare Bypass) and inserts them as relationships with
source_data='USER_SUGGESTION', so build_scored_edges.py turns them into graph
edges. Idempotent: the table's unique (source, target, type) index makes a
re-sent link a no-op, so every run simply fetches everything.

Run by rebuild_and_deploy.py before the graph build (best-effort), or by hand:
  python sync_user_links.py
"""
import json
import os
import sys

import psycopg2
import requests

sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
import pg_secret  # noqa: E402

URL = os.environ.get("USER_LINKS_URL", "https://sixdegrees.net/api/internal/user-links")
SOURCE = "USER_SUGGESTION"


def fetch_all(token):
    links, since = [], 0
    while True:
        r = requests.get(URL, params={"since_id": since}, headers={"X-Sync-Token": token,
                         "User-Agent": "sixdegrees-rebuild/1.0"}, timeout=60)
        r.raise_for_status()
        batch = r.json()["links"]
        if not batch:
            return links
        links += batch
        since = batch[-1]["id"]


def main():
    links = fetch_all(pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"))
    if not links:
        print("user links: none approved yet")
        return
    rows = []
    for l in links:
        ev = json.dumps({"source": SOURCE, "user_link_id": l["id"], "submitter": l.get("submitter_email"),
                         "approved_at": l.get("approved_at"), "evidence": l.get("evidence")}, separators=(",", ":"))
        rows.append((None, l["subject"], l.get("subject_type") or "PERSON", None, l["object"],
                     l.get("object_type") or "PERSON", l["predicate"], SOURCE, ev))
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   rows, page_size=1000)
    added = cur.rowcount
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    print(f"user links: {len(links)} approved on the site, {added} new; {cur.fetchone()[0]} {SOURCE} rows in total")


if __name__ == "__main__":
    main()
