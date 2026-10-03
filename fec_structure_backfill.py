"""FEC structure backfill: candidates <-> their committees, corporate PACs ->
sponsoring company, committee treasurers (Phase 0 of the FEC plan, 2026-10-03).

Before this, a committee node such as "BERNIE 2016" wasn't linked to Bernie
Sanders, so donations stopped at the committee. Sources, cycles 2008-2026:
  cn{yy}.zip   candidates          CAND_ID | CAND_NAME "LAST, FIRST MIDDLE" | ...
  ccl{yy}.zip  candidate-committee CAND_ID | ... | CMTE_ID | CMTE_TP | CMTE_DSGN | ...
  cm{yy}.zip   committees          CMTE_ID | CMTE_NM | TRES_NM | ... | CONNECTED_ORG_NM (13) | CAND_ID (14)

Rows written (source_data='FEC_STRUCTURE', reversible:
DELETE FROM relationships WHERE source_data='FEC_STRUCTURE'):
  candidate  CANDIDATE_COMMITTEE  committee   (ccl linkage: principal/authorized)
  candidate  LEADERSHIP_PAC       committee   (cm designation D with a CAND_ID)
  committee  PAC_SPONSOR          company     (cm CONNECTED_ORG_NM; mostly corporate/union PACs)
  treasurer  TREASURER            committee   (cm TRES_NM)

Names: committees use data/fec_committee_names.json.gz (the same map the graph
build uses for donation targets, so the nodes meet). Candidates use their
Wikidata label when Wikidata records the FEC candidate id (P1839), so
"SANDERS, BERNARD" lands on the existing "Bernie Sanders" node; otherwise
"First Middle Last".

  python fec_structure_backfill.py            # download + dry run (counts, samples)
  python fec_structure_backfill.py --commit   # write to Postgres
"""
import gzip
import io
import json
import os
import re
import sys
import time
import zipfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
BULK = os.environ.get("FEC_BULK_DIR", r"C:\Users\johnk\graphfinder-clean\data\fec_bulk")
CYCLES = list(range(2008, 2027, 2))
SOURCE = "FEC_STRUCTURE"
UA = {"User-Agent": "Mozilla/5.0 (sixdegrees research)"}
SKIP_ORGS = {"", "NONE", "N/A", "NA", "NONE.", "-"}


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def fetch(kind, year):
    yy = str(year)[2:]
    path = os.path.join(BULK, str(year), f"{kind}{yy}.zip")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = requests.get(f"https://www.fec.gov/files/bulk-downloads/{year}/{kind}{yy}.zip", headers=UA, timeout=300)
        r.raise_for_status()
        with open(path, "wb") as f:
            f.write(r.content)
    return path


def rows_of(path):
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as f:
            for line in io.TextIOWrapper(f, encoding="latin-1"):
                yield line.rstrip("\r\n").split("|")


_SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}
_TITLES = {"MR", "MRS", "MS", "DR", "HON", "REV", "SEN", "REP", "GOV"}


def person_name(fec_name):
    """'SANDERS, BERNARD' -> 'Bernard Sanders'; 'SMITH, JOHN A JR' -> 'John A Smith Jr'."""
    last, _, rest = (fec_name or "").partition(",")
    last = last.strip()
    parts = [p.strip(".") for p in rest.replace(".", ". ").split()]
    parts = [p for p in parts if p.upper().strip(".") not in _TITLES]
    suffix = [p for p in parts if p.upper() in _SUFFIX]
    given = [p for p in parts if p.upper() not in _SUFFIX]
    if not last or not given:
        return None
    words = given + last.split() + suffix
    out = " ".join(w if w.upper() in _SUFFIX - {"JR", "SR"} else _cap(w) for w in words)
    return re.sub(r"\s+", " ", out).strip()


def _cap(word):
    """Capitalize a name part, including Mc/O'/hyphenated: MCCAIN -> McCain."""
    w = "-".join(p.capitalize() for p in word.split("-"))
    if w.startswith("Mc") and len(w) > 2:
        w = "Mc" + w[2:].capitalize()
    if w.startswith("O'") and len(w) > 2:
        w = "O'" + w[2:].capitalize()
    return w


def wikidata_fec_labels():
    """FEC candidate id -> Wikidata English label (P1839)."""
    q = """SELECT ?fec ?label WHERE { ?item wdt:P1839 ?fec .
             ?item rdfs:label ?label . FILTER(LANG(?label) IN ("en", "mul")) }"""
    r = requests.get("https://query.wikidata.org/sparql", params={"query": q, "format": "json"},
                     headers={"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)"}, timeout=300)
    r.raise_for_status()
    out = {}
    for b in r.json()["results"]["bindings"]:
        fec, label = b["fec"]["value"], b["label"]["value"]
        if fec not in out or b["label"].get("xml:lang") == "en":
            out[fec] = label
    return out


def build_rows():
    with gzip.open(os.path.join(HERE, "data", "fec_committee_names.json.gz"), "rt", encoding="utf-8") as f:
        cmte_name = json.load(f)
    wd = wikidata_fec_labels()
    log(f"Wikidata labels for {len(wd):,} FEC candidate ids")
    cand_name, ccl, cm = {}, set(), {}
    for year in CYCLES:
        for r in rows_of(fetch("cn", year)):
            if len(r) > 1 and r[0]:
                cand_name[r[0]] = wd.get(r[0]) or person_name(r[1]) or cand_name.get(r[0])
        for r in rows_of(fetch("ccl", year)):
            if len(r) > 5 and r[0] and r[3] and r[5] in ("P", "A"):
                ccl.add((r[0], r[3], r[5]))
        for r in rows_of(fetch("cm", year)):
            if len(r) > 14 and r[0]:
                cm[r[0]] = r          # newest cycle wins
        log(f"  {year}: {len(cand_name):,} candidates, {len(ccl):,} links, {len(cm):,} committees")

    rows = []
    def add(s, st, t, tt, rel, ev):
        if s and t and s.lower() != t.lower():
            rows.append((s, st, t, tt, rel, json.dumps(ev, separators=(",", ":"))))
    for cand, cmte, dsgn in sorted(ccl):
        add(cand_name.get(cand), "PERSON", cmte_name.get(cmte), "ORG", "CANDIDATE_COMMITTEE",
            {"source": SOURCE, "cand_id": cand, "cmte_id": cmte, "designation": dsgn})
    for cmte, r in cm.items():
        name = cmte_name.get(cmte)
        if r[8] == "D" and r[14]:
            add(cand_name.get(r[14]), "PERSON", name, "ORG", "LEADERSHIP_PAC",
                {"source": SOURCE, "cand_id": r[14], "cmte_id": cmte})
        org = r[13].strip()
        if org.upper() not in SKIP_ORGS and org.upper() != (name or "").upper():
            add(name, "ORG", org, "ORG", "PAC_SPONSOR", {"source": SOURCE, "cmte_id": cmte, "org_type": r[12]})
        tres = person_name(r[2])
        if tres:
            add(tres, "PERSON", name, "ORG", "TREASURER", {"source": SOURCE, "cmte_id": cmte})
    return rows


def main():
    commit = "--commit" in sys.argv
    rows = build_rows()
    seen, uniq = set(), []
    for row in rows:
        key = (row[0].lower(), row[2].lower(), row[4])
        if key not in seen:
            seen.add(key)
            uniq.append(row)
    by_rel = {}
    for row in uniq:
        by_rel[row[4]] = by_rel.get(row[4], 0) + 1
    log(f"{len(uniq):,} rows: " + ", ".join(f"{k} {v:,}" for k, v in sorted(by_rel.items(), key=lambda kv: -kv[1])))
    for probe in ("Bernie Sanders", "Barack Obama", "Donald Trump", "Nancy Pelosi", "MICROSOFT CORPORATION"):
        hits = [r for r in uniq if r[0].lower() == probe.lower() or r[2].lower() == probe.lower()][:4]
        log(f"  {probe}: " + "; ".join(f"{r[0]} -{r[4]}-> {r[2]}" for r in hits))
    if not commit:
        log("dry run -- pass --commit to write")
        return
    import psycopg2
    from psycopg2.extras import execute_values
    sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
    import pg_secret
    conn = psycopg2.connect(os.environ.get("DATABASE_URL") or pg_secret.load_secret("DATABASE_URL"))
    cur = conn.cursor()
    execute_values(cur, "INSERT INTO relationships (source_id, source_name, source_type, target_id, target_name, "
                        "target_type, relation_type, source_data, evidence) VALUES %s ON CONFLICT DO NOTHING",
                   [(None, s, st, None, t, tt, rel, SOURCE, ev) for s, st, t, tt, rel, ev in uniq], page_size=5000)
    conn.commit()
    cur.execute("SELECT count(*) FROM relationships WHERE source_data = %s", (SOURCE,))
    log(f"committed. rows tagged {SOURCE}: {cur.fetchone()[0]:,}")


if __name__ == "__main__":
    main()
