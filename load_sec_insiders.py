"""Load SEC Form 3/4/5 directors and officers into relationships (2026-10-20).

source_data = SEC_FORM_345. One row per insider, company and role:
relation_type DIRECTOR, or OFFICER (<title>) as the old Form 4 harvester
wrote them, so relation_categories files them as it does today. Evidence
keeps the SEC IDs (owner and issuer CIK) and the first and last filing dates.

Names: SEC writes insiders surname first ('Barra Mary T'); they are flipped
(sec_insiders.flip_name) and then attached to an existing graph node of the
same spelling, ignoring letter case, so 'MARY T BARRA' and 'Mary T Barra'
don't become two people. Companies are matched the same way (one name per
company: its latest).

  python load_sec_insiders.py            # dry run: counts and samples, no writes
  python load_sec_insiders.py --commit   # insert (ON CONFLICT DO NOTHING)

The old SEC_FORM_4 rows (a 3-filings-per-company harvest with no dates) are
left alone here; the dry run reports how many the new rows cover, for a
separate, backed-up retirement step.
"""
import argparse
import collections
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")   # pg_secret
import sec_insiders as si  # noqa: E402

SOURCE = "SEC_FORM_345"
SEARCH_INDEX = os.path.join(HERE, "webapp", "data", "search_index.json.gz")
OUT_TSV = os.environ.get("SEC_INSIDERS_PLAN") or os.path.join(os.path.dirname(si.cache_dir()), "sec_insiders_load_plan.tsv.gz")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def graph_names():
    """lower-case name -> the graph's own spelling (the best-connected one)."""
    best = {}
    for e in json.load(gzip.open(SEARCH_INDEX)):
        k = e["canonical"].lower()
        if k not in best or e.get("degree", 0) > best[k][1]:
            best[k] = (e["canonical"], e.get("degree", 0))
    return {k: v[0] for k, v in best.items()}


def plan(names):
    merged, latest = si.load_all()
    cutoff = si._dtm.date.fromisoformat(latest) - si._dtm.timedelta(days=si.CURRENT_DAYS)
    rows, stats = [], collections.Counter()
    people, people_matched, orgs, orgs_matched = set(), set(), set(), set()
    for (owner_cik, issuer_cik), r in sorted(merged.items()):
        person = si.flip_name(r["owner_name"])
        org = r["issuer_name"]
        if not person or not org:
            stats["skipped_no_name"] += 1
            continue
        if si.is_entity(r["owner_name"]):   # a fund or parent company named as director
            stats["skipped_entity"] += 1
            continue
        # the graph's spelling, unless it's all caps and ours isn't (case doesn't split nodes)
        p = names.get(person.lower())
        if p and p.isupper() and not person.isupper():
            p = person
        o = names.get(org.lower())
        people.add(owner_cik)
        orgs.add(issuer_cik)
        if p:
            people_matched.add(owner_cik)
        if o:
            orgs_matched.add(issuer_cik)
        ev = {"source": SOURCE, "owner_cik": owner_cik, "issuer_cik": issuer_cik, "sec_name": r["owner_name"],
              "first_filing": r["first"], "last_filing": r["last"],
              "current": r["last"] >= cutoff.isoformat()}
        roles = []
        if "Director" in r["roles"]:
            roles.append("DIRECTOR")
        if "Officer" in r["roles"]:
            roles.append(f"OFFICER ({r['title']})" if r["title"] else "OFFICER")
        for rt in roles:
            rows.append((owner_cik, p or person, "PERSON", issuer_cik, o or org, "COMPANY", rt, SOURCE,
                         json.dumps(dict(ev, title=r["title"]) if rt != "DIRECTOR" else ev, sort_keys=True)))
            stats["rows_" + rt.split(" ")[0]] += 1
    stats.update(pairs=len(merged), rows=len(rows), people=len(people), people_matched=len(people_matched),
                 companies=len(orgs), companies_matched=len(orgs_matched))
    return rows, stats, latest


def connect(readonly=True):
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        import pg_secret
        url = pg_secret.load_secret("DATABASE_URL")
    conn = psycopg2.connect(url)
    conn.set_session(readonly=readonly)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '3600s'")
    return conn


def existing(conn, rows, batch=1000):
    """Rows whose (source_name, target_name, relation_type) is already in the
    table, by any source (the unique index would skip them)."""
    found = collections.Counter()
    keys = sorted({(r[1], r[4], r[6]) for r in rows})
    with conn.cursor() as cur:
        for i in range(0, len(keys), batch):
            chunk = keys[i:i + batch]
            cur.execute("SELECT source_data FROM relationships WHERE (md5(source_name), md5(target_name), relation_type) "
                        "IN (SELECT md5(a), md5(b), c FROM unnest(%s::text[], %s::text[], %s::text[]) AS t(a, b, c))",
                        ([k[0] for k in chunk], [k[1] for k in chunk], [k[2] for k in chunk]))
            for (src,) in cur.fetchall():
                found[src or ""] += 1
    return found


def old_form4_coverage(conn, rows):
    """How many old SEC_FORM_4 rows (surname-first names, issuer CIK in
    target_id) have the same insider and company in the new rows."""
    new = {(si.flip_name(json.loads(r[8])["sec_name"]).lower(), r[3].lstrip("0")) for r in rows}
    covered = total = 0
    with conn.cursor() as cur:
        cur.execute("SELECT source_name, target_id FROM relationships WHERE source_data = 'SEC_FORM_4'")
        for name, cik in cur.fetchall():
            total += 1
            if (si.flip_name(name or "").lower(), (cik or "").lstrip("0")) in new:
                covered += 1
    return covered, total


def write_plan(rows):
    with gzip.open(OUT_TSV, "wt", encoding="utf-8", newline="") as f:
        f.write("source_id\tsource_name\tsource_type\ttarget_id\ttarget_name\ttarget_type\trelation_type\tsource_data\tevidence\n")
        for r in rows:
            f.write("\t".join(str(c).replace("\t", " ") for c in r) + "\n")


def commit(rows, batch=10000):
    from psycopg2.extras import execute_values
    conn = connect(readonly=False)
    n = 0
    with conn.cursor() as cur:
        for i in range(0, len(rows), batch):
            execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                                "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                           rows[i:i + batch], page_size=batch)   # one page, so rowcount covers the batch
            n += cur.rowcount
            conn.commit()
            log(f"inserted {n:,} of {min(i + batch, len(rows)):,}")
    conn.close()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()
    log("reading graph names")
    names = graph_names()
    rows, stats, latest = plan(names)
    log(f"filings to {latest}")
    for k in ("pairs", "rows", "rows_DIRECTOR", "rows_OFFICER", "people", "people_matched", "companies",
              "companies_matched", "skipped_no_name", "skipped_entity"):
        log(f"  {k}: {stats[k]:,}")
    write_plan(rows)
    log(f"plan written to {OUT_TSV}")
    for r in rows:
        if r[3].lstrip("0") == "1467858" and json.loads(r[8])["current"]:
            log(f"  e.g. {r[1]} | {r[6]} | {r[4]}")
    conn = connect()
    found = existing(conn, rows)
    log(f"already in relationships (would be skipped): {sum(found.values()):,} {dict(found)}")
    covered, total = old_form4_coverage(conn, rows)
    log(f"old SEC_FORM_4 rows covered by the new rows: {covered:,} of {total:,}")
    conn.close()
    if not args.commit:
        log("dry run -- nothing written. Pass --commit to insert.")
        return
    n = commit(rows)
    log(f"done: {n:,} rows inserted")


if __name__ == "__main__":
    main()
