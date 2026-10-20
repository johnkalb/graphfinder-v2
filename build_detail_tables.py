"""Export the detail tables for the question menu (private-service spec Phase 2, 2026-10-19).

Writes webapp/data/detail/<table>.tsv.gz -- see webapp/detail_tables.py for
what each table holds and how the web server loads them. Runs in the nightly
rebuild after build_crawlie_facts.py (it reads that step's verified_people
list); rebuild_and_deploy.py ships a changed table at most once a week.

  python build_detail_tables.py                 # all tables, from cwd's webapp/data
  python build_detail_tables.py donors vendors  # just these

Every query sorts in Postgres with COLLATE "C", so the same data always gives
byte-identical files. Read-only.

What is kept, and why:
  donors      the top 100 donors of each committee (by total over all cycles),
              plus every donation by a verified public figure. Not all 4M
              rows: the menu asks "who are X's biggest donors" and "where does
              <public figure> give", nothing else. City and ZIP are dropped;
              state, employer and occupation are kept (they tell namesakes apart).
  pac_flows   all of FEC_PAC (1M committee -> committee/candidate rows)
  vendors     all of FEC_SPEND (organizations paid $10K+ by a committee)
  leadership  every person <-> organization row whose relation type the app
              categorises as a board, officer, executive or trustee role
              (relation_categories.categorize), titles tidied for display;
              start/end years and current/former where the source says
              (LittleSis), plus SEC Form 3/4/5 directors and officers with
              first and last filing years (sec_insiders.py, 2026-10-20)
  lobbying    LDA lobbyist -> client rows with registrant, year, issue and the
              lobbyist's former government posts (public in LDA filings)
  insider_trades  SEC Form 3/4/5 stock trades by directors and officers, per
              insider, company, year and trade code (sec_insiders.py, 2026-10-20)
"""
import gzip
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "webapp"))
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
import detail_tables as dt  # noqa: E402
import sec_insiders  # noqa: E402
from relation_categories import categorize  # noqa: E402

TOP_DONORS = 100
LEADERSHIP_CATEGORIES = {"CO_DIRECTOR", "CO_OFFICER", "CO_EXECUTIVE", "ADVISORY"}
# person-person "co-" rows, and OWNERSHIP (dropped from the graph as noisy)
LEADERSHIP_SKIP = {"CO_DIRECTOR", "CO_OFFICER", "CO_EXECUTIVE", "OWNERSHIP",
                   "CANDIDATE_COMMITTEE", "LEADERSHIP_PAC"}   # those two: the committees table
PAC_KINDS = {"PAC_CONTRIBUTION": "contribution", "COMMITTEE_TRANSFER": "transfer",
             "INDEPENDENT_EXPENDITURE_FOR": "spent for", "INDEPENDENT_EXPENDITURE_AGAINST": "spent against"}
GITHUB_LIMIT = 95_000_000   # GitHub refuses files over 100 MB


def log(msg):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def connect():
    import psycopg2
    url = os.environ.get("DATABASE_URL")
    if not url:
        import pg_secret
        url = pg_secret.load_secret("DATABASE_URL")
    conn = psycopg2.connect(url)
    conn.set_session(readonly=True)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = '3600s'")
    return conn


def stream(conn, sql, args=()):
    """Server-side cursor: rows arrive in batches, never all at once."""
    with conn.cursor(name=f"detail_{time.time_ns()}") as cur:
        cur.itersize = 20000
        cur.execute(sql, args)
        yield from cur


def ev(s):
    if not s or not s.startswith("{"):
        return {}
    try:
        return json.loads(s)
    except ValueError:
        return {}


def cycles(lst):
    return ",".join(str(c) for c in lst or [])


# --- tables ----------------------------------------------------------------------
def committees(conn, _):
    sql = """
        SELECT * FROM (
        SELECT evidence::jsonb->>'cmte_id' AS cmte_id,
               CASE WHEN relation_type = 'PAC_SPONSOR' THEN source_name ELSE target_name END AS committee,
               CASE WHEN relation_type = 'PAC_SPONSOR' THEN target_name ELSE source_name END AS linked,
               CASE relation_type WHEN 'CANDIDATE_COMMITTEE' THEN 'candidate'
                                  WHEN 'LEADERSHIP_PAC' THEN 'leadership_pac' ELSE 'sponsor' END AS role,
               coalesce(evidence::jsonb->>'cand_id', '') AS cand_id
        FROM relationships
        WHERE source_data = 'FEC_STRUCTURE'
          AND relation_type IN ('CANDIDATE_COMMITTEE', 'LEADERSHIP_PAC', 'PAC_SPONSOR')) s
        ORDER BY cmte_id COLLATE "C", role COLLATE "C", linked COLLATE "C", committee COLLATE "C",
                 cand_id COLLATE "C" """
    return stream(conn, sql)


def cmte_totals(conn, _):
    sql = """
        SELECT * FROM (
        SELECT evidence::jsonb->>'recipient' AS cmte_id, max(target_name COLLATE "C") AS committee,
               count(*) AS donors, round(sum((evidence::jsonb->>'total_usd')::numeric), 2)::float AS usd
        FROM relationships WHERE source_data = 'FEC_INDIV'
        GROUP BY 1) s ORDER BY cmte_id COLLATE "C" """
    return stream(conn, sql)


def donors(conn, verified):
    sql = f"""
        WITH d AS (
            SELECT source_name AS donor, CASE WHEN source_type = 'PERSON' THEN 'P' ELSE 'O' END AS donor_type,
                   target_name AS committee, evidence::jsonb AS e
            FROM relationships WHERE source_data = 'FEC_INDIV'),
        r AS (
            SELECT *, (e->>'total_usd')::float AS usd,
                   row_number() OVER (PARTITION BY e->>'recipient'
                                      ORDER BY (e->>'total_usd')::float DESC, donor COLLATE "C") AS rk
            FROM d)
        SELECT e->>'recipient', committee, donor, donor_type, usd, (e->>'contributions')::int, e->'cycles',
               coalesce(e->>'employer', ''), coalesce(e->>'occupation', ''), coalesce(e->>'state', '')
        FROM r
        WHERE rk <= {TOP_DONORS} OR lower(donor) = ANY(%s)
        ORDER BY e->>'recipient' COLLATE "C", usd DESC, donor COLLATE "C", committee COLLATE "C" """
    for row in stream(conn, sql, (sorted(verified),)):
        row = list(row)
        row[6] = cycles(row[6])
        yield row


def pac_flows(conn, _):
    sql = """
        SELECT source_name, target_name, relation_type, evidence
        FROM relationships WHERE source_data = 'FEC_PAC'
        ORDER BY source_name COLLATE "C", target_name COLLATE "C", relation_type COLLATE "C" """
    for giver, recipient, rel, e in stream(conn, sql):
        e = ev(e)
        yield (giver, recipient, PAC_KINDS.get(rel, rel.lower()), e.get("total_usd"),
               e.get("transactions"), cycles(e.get("cycles")))


def vendors(conn, _):
    sql = """
        SELECT source_name, target_name, evidence
        FROM relationships WHERE source_data = 'FEC_SPEND'
        ORDER BY source_name COLLATE "C", target_name COLLATE "C" """
    for payer, payee, e in stream(conn, sql):
        e = ev(e)
        yield (payer, payee, e.get("total_usd"), e.get("payments"), cycles(e.get("cycles")), e.get("purpose"))


def relation_types(conn):
    """Every distinct relation_type, by a loose index scan (85K values in 387M rows)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH RECURSIVE t AS (
                (SELECT relation_type FROM relationships WHERE relation_type IS NOT NULL
                 ORDER BY relation_type LIMIT 1)
                UNION ALL
                SELECT (SELECT relation_type FROM relationships WHERE relation_type > t.relation_type
                        ORDER BY relation_type LIMIT 1)
                FROM t WHERE t.relation_type IS NOT NULL)
            SELECT relation_type FROM t WHERE relation_type IS NOT NULL""")
        return [r[0] for r in cur.fetchall()]


def leadership_types(all_types):
    out = []
    for rt in all_types:
        if rt in LEADERSHIP_SKIP:
            continue
        cat = categorize(rt)
        if cat in LEADERSHIP_CATEGORIES or (cat == "MEMBERSHIP" and rt.upper().startswith("POSITION")):
            out.append(rt)
    return out


_SMALL = {"of", "and", "the", "for", "to", "in", "at", "on"}
_UPPER = {"ceo", "cfo", "coo", "cio", "cto", "evp", "svp", "vp", "llc", "lp", "gp", "us", "usa"}


def pretty_title(rt):
    """'POSITION (BOARD MEMBER)' -> 'Board member'; 'DIRECTOR (Chief Executive Officer)' ->
    'Director (Chief Executive Officer)'; 'BOARD_MEMBER' -> 'Board member'."""
    s = rt.strip()
    if s.upper().startswith("POSITION (") and s.endswith(")"):
        s = s[len("POSITION ("):-1]
    s = s.replace("_", " ").strip()
    head, sep, rest = s.partition("(")
    if head.strip():
        # tidy the role word(s) always; a parenthetical (an SEC filer's own wording) only if all caps
        words = head.lower().split()
        head = " ".join(w.upper() if w in _UPPER else w for w in words)
        head = head[:1].upper() + head[1:] + (" " if sep else "")
        if rest.isupper():
            rest = " ".join(w.upper() if w.strip(").,") in _UPPER else w for w in rest.lower().split())
        s = head + sep + rest
    return s[:120]


def _year(e):
    """The year a row is on file for: a year field, Form 4's filing_date, or
    the TEOS file it came from ('2022/2022_TEOS_XML_01A.zip')."""
    for k in ("year", "filing_year", "tax_year", "fiscal_year", "period_year", "filing_date", "filing"):
        v = _y(e.get(k))
        if v:
            return v
    return ""


def _y(v):
    """'1998-00-00' / '2010-06-01' -> '1998' / '2010' ('' if none)."""
    v = str(v or "")[:4]
    return v if v.isdigit() and v > "1800" else ""


def _dates(e):
    """(start, end, current) from a row's evidence, 2026-10-20. LittleSis
    gives start_date/end_date where known, and its description says "has a
    position" (current), "had a position" (former) or "has/had" (unknown)."""
    start, end = _y(e.get("start_date")), _y(e.get("end_date"))
    desc = e.get("description") or ""
    if end or " had a position" in desc:
        current = "no"
    elif " has a position" in desc:
        current = "yes"
    else:
        current = ""
    return start, end, current


def leadership(conn, _):
    types = leadership_types(relation_types(conn))
    log(f"leadership: {len(types):,} relation types")
    sql = """
        SELECT * FROM (
        SELECT CASE WHEN source_type = 'PERSON' THEN target_name ELSE source_name END AS org,
               CASE WHEN source_type = 'PERSON' THEN source_name ELSE target_name END AS person,
               relation_type, coalesce(source_data, '') AS source_data, evidence
        FROM relationships
        WHERE relation_type = ANY(%s)
          AND (coalesce(source_type, '') = 'PERSON') <> (coalesce(target_type, '') = 'PERSON')) s
        ORDER BY org COLLATE "C", person COLLATE "C", relation_type COLLATE "C", source_data COLLATE "C" """
    last = None
    for org, person, rt, src, e in stream(conn, sql, (types,)):
        if not org or not person:
            continue
        if src == "SEC_FORM_4":
            person = sec_insiders.flip_name(person)   # 'Barra Mary T' -> 'Mary T Barra'
        e = ev(e)
        row = (org, person, pretty_title(rt), src, _year(e)) + _dates(e)
        if row != last:
            yield row
            last = row
    # directors and officers with first/last filing dates (SEC Forms 3/4/5 since
    # 2006); a newly published quarter is fetched first, the cache used if that fails
    try:
        sec_insiders.fetch(log=log)
    except Exception as ex:
        log(f"WARNING: sec_insiders fetch failed ({type(ex).__name__}: {ex}) -- using the cached quarters")
    yield from sec_insiders.rows(log=log)


def lobbying(conn, _):
    sql = """
        SELECT source_name, target_name, evidence
        FROM relationships WHERE source_data = 'LDA_LOBBYISTS' AND relation_type = 'LOBBYING'
        ORDER BY target_name COLLATE "C", source_name COLLATE "C", id"""
    for lobbyist, client, e in stream(conn, sql):
        e = ev(e)
        yield (lobbyist, client, e.get("registrant") or "", str(e.get("filing_year") or ""),
               e.get("issue") or "", e.get("covered_position") or "")


def insider_trades(conn, _):
    """SEC Form 3/4/5 stock trades by directors and officers (no database read:
    the SEC data sets, summarised by sec_insiders.py; new quarters fetched first)."""
    try:
        sec_insiders.fetch_trades(log=log)
    except Exception as ex:
        log(f"WARNING: sec_insiders trades fetch failed ({type(ex).__name__}: {ex}) -- using the cached quarters")
    return sec_insiders.trade_rows(log=log)


BUILDERS = {"committees": committees, "cmte_totals": cmte_totals, "donors": donors,
            "pac_flows": pac_flows, "vendors": vendors, "leadership": leadership, "lobbying": lobbying,
            "insider_trades": insider_trades}


def load_verified(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return {n.lower() for n in json.load(f)}


def main():
    want = [a for a in sys.argv[1:] if not a.startswith("-")] or list(BUILDERS)
    unknown = set(want) - set(BUILDERS)
    if unknown:
        sys.exit(f"unknown table(s): {', '.join(sorted(unknown))}")
    out_dir = os.environ.get("DETAIL_OUT_DIR", os.path.join("webapp", "data", "detail"))
    os.makedirs(out_dir, exist_ok=True)
    verified = load_verified(os.environ.get("VERIFIED_PEOPLE", os.path.join("webapp", "data", "verified_people.json.gz")))
    log(f"{len(verified):,} verified public figures")
    conn = connect()
    failed = []
    for table in want:
        t0 = time.time()
        path = os.path.join(out_dir, dt.tsv_name(table))
        try:
            n = dt.write_tsv(path, table, BUILDERS[table](conn, verified))
        except Exception as e:
            conn.rollback()
            failed.append(table)
            log(f"FAILED {table}: {type(e).__name__}: {e}")
            continue
        size = os.path.getsize(path)
        warn = "  TOO BIG FOR GITHUB -- will not be deployed" if size > GITHUB_LIMIT else ""
        log(f"{table}: {n:,} rows, {size / 1e6:.1f} MB, {time.time() - t0:.0f}s{warn}")
    conn.close()
    if failed:
        sys.exit(f"failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
