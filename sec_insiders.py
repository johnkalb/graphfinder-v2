"""Directors and officers of public companies, with dates, from the SEC's
insider-transaction data sets (Forms 3, 4 and 5, quarterly since 2006), 2026-10-20.

Why: the board rows in the detail tables had no dates ("who is on the GM
board" got LittleSis rows from ~2015 mixed with current directors). Each
Form 3/4/5 filing names the insider, their role at the company (director,
officer and title) and a filing date, so first and last filing dates per
(insider, company) say who is on file and how recently.

  python sec_insiders.py            # fetch any quarters not yet summarised
  python sec_insiders.py --show GM  # print the rows for an issuer name match

Each quarter's zip is downloaded once and reduced to a small summary
(<cache>/<YYYY>q<N>.tsv.gz: one row per insider and company for that quarter);
the zip is then deleted. Only Director and Officer rows are kept, and only
name, role, title, company and dates: the owners' street addresses in the
data set are never read. The cache lives outside the repo
(SEC_INSIDERS_DIR, default %LOCALAPPDATA%\\sixdegrees\\sec_insiders).

build_detail_tables.leadership() calls rows() to add them as source
SEC_FORM_345.
"""
import csv
import datetime as _dtm
import gzip
import io
import os
import string
import sys
import time
import zipfile

URL = "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{q}_form345.zip"
FIRST_YEAR = 2006
HEADERS = {"User-Agent": "sixdegrees research John.kalb@gmail.com"}
SOURCE = "SEC_FORM_345"
CURRENT_DAYS = 455   # a filing in the last 15 months of the data: directors file at least yearly
COLS = ["owner_cik", "owner_name", "issuer_cik", "issuer_name", "roles", "title", "first", "last"]


def cache_dir():
    d = os.environ.get("SEC_INSIDERS_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "sixdegrees", "sec_insiders")
    os.makedirs(d, exist_ok=True)
    return d


def quarters(today=None):
    """Every finished quarter from 2006q1, oldest first."""
    today = today or _dtm.date.today()
    cur = (today.year, (today.month - 1) // 3 + 1)
    out = []
    for y in range(FIRST_YEAR, today.year + 1):
        for q in range(1, 5):
            if (y, q) < cur:
                out.append(f"{y}q{q}")
    return out


def _date(s):
    """'31-MAR-2026' -> '2026-03-31' ('' if unparseable)."""
    try:
        return _dtm.datetime.strptime(s.strip(), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


def _tsv(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"),
                                  delimiter="\t", quoting=csv.QUOTE_NONE)


def summarise(zip_bytes):
    """One quarter's zip -> {(owner_cik, issuer_cik): row dict}."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    subs = {}
    for r in _tsv(z, "SUBMISSION.tsv"):
        d = _date(r.get("FILING_DATE") or "")
        if d and r.get("ISSUERCIK"):
            subs[r["ACCESSION_NUMBER"]] = (r["ISSUERCIK"].strip(), (r.get("ISSUERNAME") or "").strip(), d)
    out = {}
    for r in _tsv(z, "REPORTINGOWNER.tsv"):
        rel = r.get("RPTOWNER_RELATIONSHIP") or ""
        roles = {x.strip() for x in rel.split(",")} & {"Director", "Officer"}
        sub = subs.get(r["ACCESSION_NUMBER"])
        if not roles or not sub:
            continue
        issuer_cik, issuer_name, d = sub
        key = ((r.get("RPTOWNERCIK") or "").strip(), issuer_cik)
        title = (r.get("RPTOWNER_TITLE") or "").strip() if "Officer" in roles else ""
        cur = out.get(key)
        if cur is None:
            out[key] = {"owner_cik": key[0], "owner_name": (r.get("RPTOWNERNAME") or "").strip(),
                        "issuer_cik": issuer_cik, "issuer_name": issuer_name, "roles": roles,
                        "title": title, "first": d, "last": d}
        else:
            cur["roles"] |= roles
            cur["first"] = min(cur["first"], d)
            if d >= cur["last"]:
                cur["last"] = d
                cur["owner_name"] = (r.get("RPTOWNERNAME") or "").strip() or cur["owner_name"]
                cur["issuer_name"] = issuer_name or cur["issuer_name"]
                cur["title"] = title or cur["title"]
            elif title and not cur["title"]:
                cur["title"] = title
    return out


def _write_summary(path, rows):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(COLS)
        for k in sorted(rows):
            r = rows[k]
            w.writerow([r["owner_cik"], r["owner_name"], r["issuer_cik"], r["issuer_name"],
                        ",".join(sorted(r["roles"])), r["title"], r["first"], r["last"]])
    os.replace(tmp, path)


def fetch(log=print):
    """Summarise every finished quarter not yet in the cache. A quarter the
    SEC hasn't published yet (404) is tried again next time."""
    import requests
    try:
        import pip_system_certs.wrapt_requests  # noqa: F401  (Norton TLS interception on the laptop)
    except ImportError:
        pass
    d = cache_dir()
    new = 0
    for q in quarters():
        path = os.path.join(d, f"{q}.tsv.gz")
        if os.path.exists(path):
            continue
        r = requests.get(URL.format(q=q), headers=HEADERS, timeout=300)
        time.sleep(1)
        if r.status_code == 404:
            log(f"sec_insiders: {q} not published yet")
            continue
        r.raise_for_status()
        rows = summarise(r.content)
        _write_summary(path, rows)
        new += 1
        log(f"sec_insiders: {q}: {len(rows):,} insider-company rows ({len(r.content) / 1e6:.0f} MB zip)")
    return new


def load_all():
    """Merge the quarter summaries: {(owner_cik, issuer_cik): row}, plus the
    latest filing date in the data (for 'current')."""
    d = cache_dir()
    merged = {}
    issuer_name = {}   # issuer_cik -> (last date, name): one name per company, its latest
    latest = ""
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".tsv.gz"):
            continue
        with gzip.open(os.path.join(d, fn), "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                key = (r["owner_cik"], r["issuer_cik"])
                latest = max(latest, r["last"])
                if r["issuer_name"] and r["last"] >= issuer_name.get(r["issuer_cik"], ("", ""))[0]:
                    issuer_name[r["issuer_cik"]] = (r["last"], r["issuer_name"])
                roles = set(filter(None, r["roles"].split(",")))
                cur = merged.get(key)
                if cur is None:
                    merged[key] = dict(r, roles=roles)
                    continue
                cur["roles"] |= roles
                cur["first"] = min(cur["first"], r["first"])
                if r["last"] >= cur["last"]:
                    cur["last"] = r["last"]
                    cur["owner_name"] = r["owner_name"] or cur["owner_name"]
                    cur["title"] = r["title"] or cur["title"]
                elif r["title"] and not cur["title"]:
                    cur["title"] = r["title"]
    for r in merged.values():
        r["issuer_name"] = issuer_name.get(r["issuer_cik"], ("", r["issuer_name"]))[1]
    return merged, latest


# --- trades (2026-10-20) -------------------------------------------------------
# Non-derivative (stock) transactions by directors and officers, summarised per
# insider, company, year and transaction code. The codes that matter most:
# P open-market purchase, S open-market sale; also A award, M option exercise,
# F shares withheld for tax, G gift, D disposed to the issuer.
TRADE_COLS = ["owner_cik", "issuer_cik", "year", "code", "acq_disp", "n", "shares", "usd",
              "first", "last", "max_usd", "max_date"]
TRADE_CODES = {"P": "open-market purchase", "S": "open-market sale", "A": "award", "M": "option exercise",
               "F": "tax withholding", "G": "gift", "D": "disposed to issuer", "C": "conversion",
               "J": "other", "X": "option exercise", "L": "small acquisition", "W": "will/inheritance",
               "U": "tender", "I": "discretionary", "E": "expiration", "K": "swap", "H": "expiration", "Z": "trust"}


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def summarise_trades(zip_bytes):
    """One quarter's zip -> {(owner_cik, issuer_cik, year, code, acq_disp): row}."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    issuer = {r["ACCESSION_NUMBER"]: (r.get("ISSUERCIK") or "").strip() for r in _tsv(z, "SUBMISSION.tsv")}
    owners = {}
    for r in _tsv(z, "REPORTINGOWNER.tsv"):
        rel = {x.strip() for x in (r.get("RPTOWNER_RELATIONSHIP") or "").split(",")}
        if rel & {"Director", "Officer"}:
            owners.setdefault(r["ACCESSION_NUMBER"], []).append((r.get("RPTOWNERCIK") or "").strip())
    out = {}
    for r in _tsv(z, "NONDERIV_TRANS.tsv"):
        acc = r["ACCESSION_NUMBER"]
        d = _date(r.get("TRANS_DATE") or "")
        if acc not in owners or not issuer.get(acc) or not d:
            continue
        shares, price = _num(r.get("TRANS_SHARES")), _num(r.get("TRANS_PRICEPERSHARE"))
        usd = shares * price if price > 0 else 0.0
        code = (r.get("TRANS_CODE") or "").strip()[:1]
        ad = (r.get("TRANS_ACQUIRED_DISP_CD") or "").strip()[:1]
        for o in owners[acc]:
            key = (o, issuer[acc], d[:4], code, ad)
            cur = out.get(key)
            if cur is None:
                out[key] = {"n": 1, "shares": shares, "usd": usd, "first": d, "last": d,
                            "max_usd": usd, "max_date": d}
            else:
                cur["n"] += 1
                cur["shares"] += shares
                cur["usd"] += usd
                cur["first"], cur["last"] = min(cur["first"], d), max(cur["last"], d)
                if usd > cur["max_usd"]:
                    cur["max_usd"], cur["max_date"] = usd, d
    return out


def _merge_trade(cur, r):
    cur["n"] += r["n"]
    cur["shares"] += r["shares"]
    cur["usd"] += r["usd"]
    cur["first"], cur["last"] = min(cur["first"], r["first"]), max(cur["last"], r["last"])
    if r["max_usd"] > cur["max_usd"]:
        cur["max_usd"], cur["max_date"] = r["max_usd"], r["max_date"]


def fetch_trades(log=print):
    """Like fetch(), for the trade summaries (<cache>/trades/<q>.tsv.gz)."""
    import requests
    try:
        import pip_system_certs.wrapt_requests  # noqa: F401
    except ImportError:
        pass
    d = os.path.join(cache_dir(), "trades")
    os.makedirs(d, exist_ok=True)
    new = 0
    for q in quarters():
        path = os.path.join(d, f"{q}.tsv.gz")
        if os.path.exists(path):
            continue
        r = requests.get(URL.format(q=q), headers=HEADERS, timeout=300)
        time.sleep(1)
        if r.status_code == 404:
            log(f"sec_insiders trades: {q} not published yet")
            continue
        r.raise_for_status()
        rows = summarise_trades(r.content)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter="\t", lineterminator="\n")
            w.writerow(TRADE_COLS)
            for k in sorted(rows):
                v = rows[k]
                w.writerow(list(k) + [v["n"], round(v["shares"], 2), round(v["usd"], 2), v["first"], v["last"],
                                      round(v["max_usd"], 2), v["max_date"]])
        os.replace(tmp, path)
        new += 1
        log(f"sec_insiders trades: {q}: {len(rows):,} insider-company-year-code rows")
    return new


def load_trades():
    """All quarters merged: {(owner_cik, issuer_cik, year, code, acq_disp): row}."""
    d = os.path.join(cache_dir(), "trades")
    out = {}
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".tsv.gz"):
            continue
        with gzip.open(os.path.join(d, fn), "rt", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
                key = (r["owner_cik"], r["issuer_cik"], r["year"], r["code"], r["acq_disp"])
                v = {"n": int(r["n"]), "shares": _num(r["shares"]), "usd": _num(r["usd"]), "first": r["first"],
                     "last": r["last"], "max_usd": _num(r["max_usd"]), "max_date": r["max_date"]}
                if key in out:
                    _merge_trade(out[key], v)
                else:
                    out[key] = v
    return out


# --- names -------------------------------------------------------------------
_PARTICLES = {"van", "von", "de", "del", "della", "der", "den", "di", "da", "du", "la", "le",
              "st", "st.", "dos", "das", "ter", "ten", "al", "el", "bin", "mac"}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "esq", "cpa"}


def flip_name(name):
    """SEC insider names are surname first: 'Barra Mary T.' -> 'Mary T. Barra',
    'KELLY ALFRED F JR' -> 'Alfred F Kelly Jr',
    'Van Der Berg John' -> 'John Van Der Berg', 'Smith, John A' -> 'John A Smith'.
    All-caps names are put in title case."""
    s = " ".join((name or "").replace(" ", " ").split())
    if not s:
        return s
    if "," in s:
        last, _, rest = s.partition(",")
        toks = rest.split() + last.split()
    else:
        toks = s.split()
        suffix = []
        while len(toks) > 2 and toks[-1].lower().strip(".,") in _SUFFIXES:
            suffix.insert(0, toks.pop())
        if len(toks) >= 2:
            i = 1
            while i < len(toks) - 1 and toks[i - 1].lower() in _PARTICLES:
                i += 1
            toks = toks[i:] + toks[:i] + suffix
        else:
            toks += suffix
    out = " ".join(toks)
    if out.isupper():
        out = " ".join(_title_word(w) for w in out.split())
    return out


def _title_word(w):
    """'O'BRIEN' -> "O'Brien", 'MCNEILL' -> 'McNeill', 'III' -> 'III'."""
    if w.lower().strip(".,") in {"ii", "iii", "iv"}:
        return w
    w = "-".join(string.capwords(p.lower(), "'") for p in w.split("-"))
    if len(w) > 3 and w.startswith("Mc"):
        w = "Mc" + w[2:].capitalize()
    return w


def title_of(r):
    roles, title = r["roles"], (r.get("title") or "").strip()
    if "Director" in roles and title:
        return f"Director, {title}"[:120]
    if "Director" in roles:
        return "Director"
    return (title or "Officer")[:120]


def rows(log=print):
    """Leadership rows (org, person, title, source, year, start, end, current),
    sorted for byte-identical output."""
    merged, latest = load_all()
    if not merged:
        log("sec_insiders: cache is empty -- run sec_insiders.py first")
        return []
    cutoff = (_dtm.date.fromisoformat(latest) - _dtm.timedelta(days=CURRENT_DAYS)).isoformat()
    out = []
    for r in merged.values():
        org, person = r["issuer_name"], flip_name(r["owner_name"])
        if not org or not person:
            continue
        out.append((org, person, title_of(r), SOURCE, r["last"][:4], r["first"][:4], r["last"][:4],
                    "yes" if r["last"] >= cutoff else "no"))
    out.sort()
    log(f"sec_insiders: {len(out):,} director/officer rows, filings to {latest}")
    return out


def main():
    if "--show" in sys.argv:
        pat = sys.argv[sys.argv.index("--show") + 1].lower()
        for r in rows():
            if pat in r[0].lower():
                print("\t".join(r))
        return
    n = fetch()
    print(f"{n} new quarter(s) summarised in {cache_dir()}")


if __name__ == "__main__":
    main()
