#!/usr/bin/env python3
"""SEC Form D bulk harvester -- the executives, directors and promoters of
every private offering and private fund that filed a Form D, from the SEC's
quarterly Form D data sets (2008Q1 onward).

Why: there is no permitted bulk source for individual financial advisors
(FINRA BrokerCheck/IAPD terms limit compiling to investor-protection,
academic and compliance use -- checked 2026-10-07). Form D's "related
persons" are the closest public bulk equivalent: ~55K per quarter, about
two-thirds of them on pooled investment funds (fund managers, GPs), the rest
on startups and real-estate offerings. Until now SEC_FORM_D had only 1,714
rows, from per-filing XML fetches (src/data/form_d_client.py).

Rows match that client's shape: PERSON -[DIRECTOR|OFFICER|PROMOTER]->
COMPANY, target_id = issuer CIK zero-padded to 10, source_data SEC_FORM_D;
evidence carries accession, filing date, the person's city/state (for
future namesake splitting), the role clarification and industry group.

Left out, deliberately:
  - related "persons" that are companies (GP LLCs, administrators) --
    ~19% of rows; the people behind them are listed too;
  - administrator / agent / custodian roles, and anyone on more than
    MAX_ISSUERS_PER_PERSON filings in one quarter with almost nobody else
    on them: SPV-platform administrators (Sydecar-style) sign hundreds of
    unrelated SPVs a quarter alone (one person: 675 filings in 2026Q2).
    Fund-family executives on many funds with the same colleagues (PIMCO's
    team, 66 funds) are kept.

Cursor-driven like the other harvesters: harvest_cursors 'sec_form_d_bulk'
holds how many quarters (oldest first) are done; new quarters the SEC adds
are picked up from its download page.

Usage:
    python harvest_sec_form_d_bulk.py --limit 8     # next 8 quarters
    python harvest_sec_form_d_bulk.py --dry-run --quarter 2026q2
"""
import argparse
import collections
import csv
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

sys.path.insert(0, "/home/john/.hermes" if os.name != "nt" else r"C:\Users\johnk\Desktop\sixdegrees")
from src.config import SEC_USER_AGENT  # noqa: E402

SOURCE = "SEC_FORM_D"
CURSOR_SOURCE = "sec_form_d_bulk"
PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/form-d-data-sets"
HEADERS = {"User-Agent": SEC_USER_AGENT}
MAX_ISSUERS_PER_PERSON = 50  # ...and nearly alone on them -- see rows_for_quarter
ROLE_MAP = {"Executive Officer": "OFFICER", "Director": "DIRECTOR", "Promoter": "PROMOTER"}
NOT_A_PERSON = re.compile(
    r"\b(LLC|L\.?L\.?C|LP|L\.?P|LLP|INC|INCORPORATED|CORP|CORPORATION|COMPANY|CO|LTD|LIMITED|GP|"
    r"PARTNERS|PARTNERSHIP|FUND|FUNDS|CAPITAL|MANAGEMENT|ADVISORS|ADVISERS|ADVISORY|HOLDINGS|"
    r"TRUST|TRUSTEE|BANK|GROUP|VENTURES|SECURITIES|INVESTMENTS?|ASSOCIATES|SERVICES|PLC|SA|AG|"
    r"GMBH|N\.?A|SARL|BV|NV|SPV|SERIES|ADMINISTRATOR|FOUNDATION)\b", re.I)
SKIP_ROLE = re.compile(r"ADMINISTRAT|\bAGENT\b|CUSTODIAN|PLACEMENT|ESCROW|TRANSFER AGENT|PAYING", re.I)
NO_FIRST = {"", "N/A", "NA", "-", "--", "NONE", ".", "N.A."}


def quarter_urls():
    """[(quarter, url)] oldest first, from the SEC's download page."""
    page = requests.get(PAGE_URL, headers=HEADERS, timeout=60).text
    found = {}
    for href in re.findall(r'href="([^"]*form-d-data-sets/(\d{4}q[1-4])_d[^"]*\.zip)"', page):
        url, q = href
        found[q] = url if url.startswith("http") else "https://www.sec.gov" + url
    return sorted(found.items())


def fetch_zip(url):
    """The SEC serves these from two paths and returns a transient 404 now
    and then (2026q1, 2026-10-08) -- retry, trying the other path too."""
    alt = (url.replace("/structureddata/", "/datastandardsinnovation/") if "/structureddata/" in url
           else url.replace("/datastandardsinnovation/", "/structureddata/"))
    for attempt in range(4):
        for u in (url, alt):
            r = requests.get(u, headers=HEADERS, timeout=300)
            if r.status_code == 200:
                return r.content
        time.sleep(10 * (attempt + 1))
    r.raise_for_status()


def _tsv(z, suffix):
    name = next(n for n in z.namelist() if n.upper().endswith(suffix))
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace"), delimiter="\t")


def person_name(r):
    first = (r.get("FIRSTNAME") or "").strip()
    parts = [first, (r.get("MIDDLENAME") or "").strip(), (r.get("LASTNAME") or "").strip()]
    name = re.sub(r"\s+", " ", " ".join(p for p in parts if p and p.upper() not in NO_FIRST)).strip(" ,.")
    if first.upper() in NO_FIRST or not name or len(name.split()) < 2 or NOT_A_PERSON.search(name):
        return None
    if any(ch.isdigit() for ch in name):
        return None
    return name


def rows_for_quarter(content, quarter):
    """-> (relationship tuples, stats)."""
    z = zipfile.ZipFile(io.BytesIO(content))
    sub = {r["ACCESSIONNUMBER"]: r for r in _tsv(z, "FORMDSUBMISSION.TSV")}
    issuer = {}
    for r in _tsv(z, "ISSUERS.TSV"):
        a = r["ACCESSIONNUMBER"]
        if a not in issuer or (r.get("IS_PRIMARYISSUER_FLAG") or "").upper() == "YES":
            issuer[a] = r
    industry = {r["ACCESSIONNUMBER"]: r.get("INDUSTRYGROUPTYPE") for r in _tsv(z, "OFFERING.TSV")}

    stats = collections.Counter()
    cand = []
    for r in _tsv(z, "RELATEDPERSONS.TSV"):
        stats["related_persons"] += 1
        a = r["ACCESSIONNUMBER"]
        iss = issuer.get(a)
        if not iss or (sub.get(a, {}).get("TESTORLIVE") or "LIVE").upper() == "TEST":
            stats["no_issuer"] += 1
            continue
        name = person_name(r)
        if not name:
            stats["not_a_person"] += 1
            continue
        clar = (r.get("RELATIONSHIPCLARIFICATION") or "").strip()
        if SKIP_ROLE.search(clar):
            stats["admin_or_agent"] += 1
            continue
        roles = {ROLE_MAP[x] for x in (r.get("RELATIONSHIP_1"), r.get("RELATIONSHIP_2"), r.get("RELATIONSHIP_3"))
                 if x in ROLE_MAP}
        if not roles:
            stats["no_role"] += 1
            continue
        cand.append((name, iss, roles, r, clar, a))

    # SPV-platform administrators sit ALONE on hundreds of unrelated vehicles a
    # quarter (2026Q2: one person on 675 filings with 1 other person across
    # all of them); fund-family executives (PIMCO's team on 66 funds) share
    # their filings with the same colleagues. Only the first pattern is
    # dropped: many filings, almost no co-listed people.
    people_on = collections.defaultdict(set)
    for name, _, _, _, _, a in cand:
        people_on[a].add(name.lower())
    filings_of = collections.defaultdict(set)
    for a, ppl in people_on.items():
        for p in ppl:
            filings_of[p].add(a)
    hubs = set()
    for p, accs in filings_of.items():
        if len(accs) > MAX_ISSUERS_PER_PERSON:
            others = set().union(*(people_on[a] for a in accs)) - {p}
            if len(others) < 0.1 * len(accs):
                hubs.add(p)
    stats["hub_people"] = len(hubs)

    out = []
    for name, iss, roles, r, clar, a in cand:
        if name.lower() in hubs:
            stats["hub_rows"] += 1
            continue
        cik = (iss.get("CIK") or "").strip().zfill(10) or None
        ev = json.dumps({
            "source": SOURCE, "quarter": quarter, "accession": a,
            "filing_date": sub.get(a, {}).get("FILING_DATE"),
            "submission": sub.get(a, {}).get("SUBMISSIONTYPE"),
            "city": (r.get("CITY") or "").strip() or None,
            "state": (r.get("STATEORCOUNTRY") or "").strip() or None,
            "clarification": clar or None,
            "industry": industry.get(a),
        }, separators=(",", ":"))
        for role in sorted(roles):
            out.append((None, name, "PERSON", cik, iss["ENTITYNAME"].strip(), "COMPANY", role, SOURCE, ev))
    stats["rows"] = len(out)
    return out, stats


def load(conn, rows):
    from psycopg2.extras import execute_values
    with conn.cursor() as cur:
        # rowcount only covers execute_values' last page, so count RETURNING rows
        n = len(execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, "
                                    "target_name, target_type, relation_type, source_data, evidence) VALUES %s "
                                    "ON CONFLICT DO NOTHING RETURNING 1", rows, page_size=5000, fetch=True))
    conn.commit()
    return n


def cursor_get(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO harvest_cursors (source, cursor_key, cursor_value) VALUES (%s, 'quarter_idx', 0) "
                    "ON CONFLICT (source) DO NOTHING", (CURSOR_SOURCE,))
        cur.execute("SELECT cursor_value FROM harvest_cursors WHERE source=%s", (CURSOR_SOURCE,))
        v = int(cur.fetchone()[0])
    conn.commit()
    return v


def cursor_set(conn, v, status):
    with conn.cursor() as cur:
        cur.execute("UPDATE harvest_cursors SET cursor_value=%s, status=%s, updated_at=now() WHERE source=%s",
                    (v, status, CURSOR_SOURCE))
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=8, help="quarters this run")
    ap.add_argument("--dry-run", action="store_true", help="parse only, write nothing")
    ap.add_argument("--quarter", help="with --dry-run: just this quarter, e.g. 2026q2")
    args = ap.parse_args()

    quarters = quarter_urls()
    print(f"[form-d] {len(quarters)} quarters on the SEC page ({quarters[0][0]}..{quarters[-1][0]})")

    if args.dry_run:
        todo = [q for q in quarters if not args.quarter or q[0] == args.quarter][:args.limit]
        for q, url in todo:
            rows, stats = rows_for_quarter(fetch_zip(url), q)
            print(f"[form-d] {q}: {dict(stats)}")
            for r in rows[:5]:
                print("   ", r[1], "->", r[4], r[6])
        return

    import psycopg2
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    idx = cursor_get(conn)
    if idx >= len(quarters):
        print("[form-d] all quarters loaded")
        cursor_set(conn, idx, "complete")
        return
    for i in range(idx, min(idx + args.limit, len(quarters))):
        q, url = quarters[i]
        t0 = time.time()
        rows, stats = rows_for_quarter(fetch_zip(url), q)
        added = load(conn, rows)
        cursor_set(conn, i + 1, "running")
        print(f"[form-d] {q}: {len(rows)} rows, {added} new, {time.time() - t0:.0f}s  {dict(stats)}", flush=True)
        time.sleep(1)
    print(f"[form-d] cursor -> {min(idx + args.limit, len(quarters))}/{len(quarters)}")
    conn.close()


if __name__ == "__main__":
    main()
