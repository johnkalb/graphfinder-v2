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
    """-> (approved review-queue links, public my_links, removed public my_links)."""
    links, since, public, removed = [], 0, [], []
    while True:
        r = requests.get(URL, params={"since_id": since}, headers={"X-Sync-Token": token,
                         "User-Agent": "sixdegrees-rebuild/1.0"}, timeout=60)
        r.raise_for_status()
        body = r.json()
        if since == 0:
            public, removed = body.get("public_links", []), body.get("removed_links", [])
        batch = body["links"]
        if not batch:
            return links, public, removed
        links += batch
        since = batch[-1]["id"]


def main():
    links, public, removed = fetch_all(pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"))
    rows = []
    for l in links:
        ev = json.dumps({"source": SOURCE, "user_link_id": l["id"], "submitter": l.get("submitter_email"),
                         "approved_at": l.get("approved_at"), "evidence": l.get("evidence")}, separators=(",", ":"))
        rows.append((None, l["subject"], l.get("subject_type") or "PERSON", None, l["object"],
                     l.get("object_type") or "PERSON", l["predicate"], SOURCE, ev))
    # each user's PUBLIC links (my_links; private ones never leave the app)
    for l in public:
        if not l.get("owner"):
            continue
        ev = json.dumps({"source": SOURCE, "my_link_id": l["my_link_id"], "visibility": "public"}, separators=(",", ":"))
        rows.append((None, l["owner"], "PERSON", None, l["contact"], l.get("contact_type") or "PERSON",
                     l["relation"], SOURCE, ev))
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    added = 0
    if rows:
        execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                            "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                       rows, page_size=1000)
        added = cur.rowcount
    # public links deleted or made private leave the shared graph (decision 10)
    live = {(l["owner"], l["contact"], l["relation"]) for l in public}
    deleted = 0
    for l in removed:
        if not l.get("owner") or (l["owner"], l["contact"], l["relation"]) in live:
            continue
        cur.execute("DELETE FROM relationships WHERE source_data = %s AND md5(source_name) = md5(%s) "
                    "AND md5(target_name) = md5(%s) AND relation_type = %s",
                    (SOURCE, l["owner"], l["contact"], l["relation"]))
        deleted += cur.rowcount
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    print(f"user links: {len(links)} approved via review, {len(public)} public my_links; {added} new rows, "
          f"{deleted} removed; {cur.fetchone()[0]} {SOURCE} rows in total")


if __name__ == "__main__":
    main()
