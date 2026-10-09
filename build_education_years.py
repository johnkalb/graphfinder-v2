#!/usr/bin/env python3
"""Graduation year for each person-school education link, so the graph can
treat two alumni as connected only when they were there at about the same
time (user decision 2026-10-08: same-school links keep their strength only
between people whose years are within 3; everything else is weak).

Year, best source first:
  1. the record itself -- LittleSis end/start dates, CourtListener judges'
     degree year;
  2. Wikidata's P69 statement qualifiers (P582 end time, else P580 start
     time + typical length) for people with a QID (WIKIDATA_P69,
     WIKIDATA_LEGAL);
  3. Wikidata birth year (P569) + typical graduation age (22; 25 for law,
     medical, business and graduate schools).
A start year alone becomes start + 3 (law) or 4.

Writes Postgres table education_years (person, school, grad_year, basis,
source_data), names lowercased exactly as in relationships;
build_scored_edges.py looks links up there.

Usage:
    python build_education_years.py            # rebuild the table
    python build_education_years.py --dry-run  # counts only
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict

import requests

EDU_RELS = ("EDUCATION", "ALMA_MATER", "ALUMNI", "ALUMNI_OF", "EDUCATED_AT")
WDQS = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "sixdegrees-research/1.0 (john@sixdegrees.net)", "Accept": "application/sparql-results+json"}
GRAD_SCHOOL = re.compile(r"\b(LAW|MEDICAL|MEDICINE|BUSINESS|GRADUATE|DIVINITY|DENTAL|NURSING|PUBLIC POLICY|"
                         r"GOVERNMENT|JOURNALISM|SCHOOL OF PUBLIC|FUQUA|WHARTON|SLOAN|KELLOGG|BOOTH|TUCK|HAAS)\b", re.I)
_YEAR = re.compile(r"(1[89]\d\d|20\d\d)")


def _year(v):
    m = _YEAR.search(str(v or ""))
    return int(m.group(1)) if m else None


def length(school):
    return 3 if re.search(r"\bLAW\b", school, re.I) else 4


def grad_age(school):
    return 25 if GRAD_SCHOOL.search(school) else 22


def person_school(src, s, t):
    # Wikipedia rows run school -> person; everything else person -> school.
    return (t, s) if src == "WIKIPEDIA" else (s, t)


def from_record(src, ev, school):
    if src == "CL_JUDGES":
        y = _year(ev.get("year"))
        return (y, "record") if y else None
    if src == "LITTLESIS":
        end, start = _year(ev.get("end_date")), _year(ev.get("start_date"))
        if end:
            return end, "record"
        if start:
            return start + length(school), "record_start"
    return None


def wikidata_years(qids):
    """qid -> {"birth": year|None, "schools": {label_lower: (start, end)}}"""
    out = {}
    qids = sorted(qids)
    for i in range(0, len(qids), 80):
        batch = qids[i:i + 80]
        q = ("SELECT ?p ?birth ?schoolLabel ?start ?end WHERE { VALUES ?p { %s } "
             "OPTIONAL { ?p wdt:P569 ?birth } "
             "OPTIONAL { ?p p:P69 ?st . ?st ps:P69 ?school . OPTIONAL { ?st pq:P580 ?start } OPTIONAL { ?st pq:P582 ?end } } "
             "SERVICE wikibase:label { bd:serviceParam wikibase:language \"en\". } }") % " ".join("wd:" + x for x in batch)
        for attempt in range(5):
            try:
                r = requests.post(WDQS, data={"query": q}, headers=HEADERS, timeout=120)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(10 * (attempt + 1))
        else:
            print(f"[edu-years] WDQS batch {i} failed; skipped", flush=True)
            continue
        for b in r.json()["results"]["bindings"]:
            qid = b["p"]["value"].rsplit("/", 1)[-1]
            d = out.setdefault(qid, {"birth": None, "schools": {}})
            if "birth" in b:
                d["birth"] = d["birth"] or _year(b["birth"]["value"])
            if "schoolLabel" in b:
                lab = b["schoolLabel"]["value"].lower()
                st, en = _year(b.get("start", {}).get("value")), _year(b.get("end", {}).get("value"))
                old = d["schools"].get(lab, (None, None))
                d["schools"][lab] = (old[0] or st, old[1] or en)
        if (i // 80) % 25 == 0:
            print(f"[edu-years] wikidata {i + len(batch)}/{len(qids)}", flush=True)
        time.sleep(1.0)
    return out


def from_wikidata(wd, school, wd_school):
    if not wd:
        return None
    se = wd["schools"].get((wd_school or school).lower()) or wd["schools"].get(school.lower())
    if se:
        start, end = se
        if end:
            return end, "wikidata_end"
        if start:
            return start + length(school), "wikidata_start"
    if wd["birth"]:
        return wd["birth"] + grad_age(school), "birth_estimate"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import psycopg2
    from psycopg2.extras import execute_values
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur = conn.cursor()
    cur.execute("SET statement_timeout = '30min'")
    cur.execute("SELECT source_name, target_name, source_data, evidence FROM relationships WHERE relation_type IN %s",
                (EDU_RELS,))
    rows = cur.fetchall()
    print(f"[edu-years] {len(rows):,} education links")

    out, need_wd = {}, []
    for s, t, src, ev in rows:
        if not s or not t:
            continue
        person, school = person_school(src, s, t)
        try:
            ev = json.loads(ev) if ev else {}
        except ValueError:
            ev = {}
        got = from_record(src, ev, school)
        if got:
            out[(person.lower(), school.lower(), src)] = got
        elif ev.get("qid"):
            need_wd.append((person, school, src, ev["qid"], ev.get("wd_school")))

    wd = wikidata_years({q for *_, q, _ in need_wd})
    for person, school, src, qid, wd_school in need_wd:
        got = from_wikidata(wd.get(qid), school, wd_school)
        if got:
            out[(person.lower(), school.lower(), src)] = got

    basis = Counter(b for _, b in out.values())
    print(f"[edu-years] dated {len(out):,} of {len(rows):,} links; by basis: {dict(basis)}")
    if args.dry_run:
        return
    cur.execute("""CREATE TABLE IF NOT EXISTS education_years(
        person TEXT NOT NULL, school TEXT NOT NULL, source_data TEXT NOT NULL,
        grad_year INT NOT NULL, basis TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (person, school, source_data))""")
    cur.execute("TRUNCATE education_years")
    execute_values(cur, "INSERT INTO education_years (person, school, source_data, grad_year, basis) VALUES %s",
                   [(p, sc, src, y, b) for (p, sc, src), (y, b) in out.items()], page_size=5000)
    conn.commit()
    print("[edu-years] education_years written")


if __name__ == "__main__":
    main()
