"""Detail tables for the question menu (private-service spec Phase 2, 2026-10-19).

The scored graph keeps only who-is-linked-to-whom; the menu questions also need
amounts, cycles, titles, years and issues. The nightly build exports those as
sorted, gzipped TSV files (build_detail_tables.py -> webapp/data/detail/), one
per table, and the web server loads them into a local SQLite file at startup so
questions are answered with indexed lookups and almost no RAM (decision 9: the
Optiplex is never in the live request path).

  committees   cmte_id, committee, linked, role       FEC committee <-> candidate / sponsor
  cmte_totals  cmte_id, committee, donors, usd        all itemised donors of a committee
  donors       top 100 donors per committee, plus every donation by a verified public figure
  pac_flows    committee -> committee / candidate money (PAC gifts, transfers, independent spending)
  vendors      who campaigns and PACs pay (consultants, pollsters, media buyers)
  leadership   person <-> organization roles (boards, officers, executives, trustees)
  lobbying     lobbyist -> client, with the registrant firm, year, issue and former posts

Names are matched by canon_key (the graph build's merge key), so a table file
only changes when its data does, not every time node ids shift.

Who may be the subject of which question (decision 7) is the HTTP layer's job
(pathfinder.py); this module only stores and looks up.
"""
import gzip
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata

try:
    from nicknames import formal_forms
except ImportError:  # imported as webapp.detail_tables
    from webapp.nicknames import formal_forms

# --- the shared name key (same as build_scored_edges.canon_key) ----------------
_COMBINING = dict.fromkeys(range(0x300, 0x370))
_CK_KEEP = re.compile(r"[^\w ]|_")
_CK_WS = re.compile(r"\s+")


def canon_key(name):
    name = name or ""
    s = unicodedata.normalize("NFKD", name.lower()).translate(_COMBINING)
    s = _CK_WS.sub(" ", _CK_KEEP.sub("", s)).strip()
    return s if len(s) >= 2 else name.lower().strip()


# --- table specs ----------------------------------------------------------------
# cols: the TSV columns, in order ("name:type"; type defaults to TEXT)
# keys: extra columns computed at load: key column -> source column (canon_key)
# indexes: lists of columns
TABLES = {
    "committees": {
        "cols": ["cmte_id", "committee", "linked", "role", "cand_id"],
        "keys": {"committee_key": "committee", "linked_key": "linked"},
        "indexes": [["committee_key"], ["linked_key"], ["cmte_id"]],
    },
    "cmte_totals": {
        "cols": ["cmte_id", "committee", "donors:INTEGER", "usd:REAL"],
        "keys": {"committee_key": "committee"},
        "indexes": [["cmte_id"], ["committee_key"]],
    },
    "donors": {
        "cols": ["cmte_id", "committee", "donor", "donor_type", "usd:REAL", "n:INTEGER", "cycles",
                 "employer", "occupation", "state"],
        "keys": {"donor_key": "donor", "committee_key": "committee"},
        "indexes": [["cmte_id", "usd"], ["donor_key"], ["committee_key"]],
    },
    "pac_flows": {
        "cols": ["giver", "recipient", "kind", "usd:REAL", "n:INTEGER", "cycles"],
        "keys": {"giver_key": "giver", "recipient_key": "recipient"},
        "indexes": [["giver_key"], ["recipient_key"]],
    },
    "vendors": {
        "cols": ["payer", "payee", "usd:REAL", "n:INTEGER", "cycles", "purpose"],
        "keys": {"payer_key": "payer", "payee_key": "payee"},
        "indexes": [["payer_key"], ["payee_key"]],
    },
    "leadership": {
        # since/until: years held where the source says; current: yes/no/'' (2026-10-20)
        "cols": ["org", "person", "title", "source", "year", "since", "until", "current"],
        "keys": {"org_key": "org", "person_key": "person"},
        "indexes": [["org_key"], ["person_key"]],
    },
    # SEC Form 3/4/5 stock trades by directors and officers, per insider, company,
    # year and transaction code (sec_insiders.py, 2026-10-20). joint=1: a filing
    # naming several insiders (theirs together, not each); unpriced: trades left
    # out of usd because the filed price is missing or implausible
    "insider_trades": {
        "cols": ["insider", "company", "year", "code", "kind", "joint:INTEGER", "n:INTEGER", "shares:REAL",
                 "usd:REAL", "first", "last", "max_usd:REAL", "max_date", "unpriced:INTEGER"],
        "keys": {"insider_key": "insider", "company_key": "company"},
        "indexes": [["insider_key", "year"], ["company_key", "year"]],
    },
    "lobbying": {
        "cols": ["lobbyist", "client", "registrant", "year", "issue", "covered_position"],
        "keys": {"lobbyist_key": "lobbyist", "client_key": "client", "registrant_key": "registrant"},
        "indexes": [["lobbyist_key"], ["client_key"], ["registrant_key"]],
    },
}


def tsv_name(table):
    return f"{table}.tsv.gz"


def _col(spec):
    name, _, typ = spec.partition(":")
    return name, typ or "TEXT"


def clean_cell(v):
    """One TSV cell: no tabs or newlines, None -> ''."""
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return re.sub(r"[\t\r\n]+", " ", str(v)).strip()


def write_tsv(path, table, rows):
    """Write rows (tuples in TABLES[table]['cols'] order) deterministically:
    gzip header without a timestamp, so identical data gives identical bytes
    and git stores an unchanged table only once. Returns the row count."""
    ncols = len(TABLES[table]["cols"])
    n = 0
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as gz:
                gz.write(("\t".join(_col(c)[0] for c in TABLES[table]["cols"]) + "\n").encode("utf-8"))
                for r in rows:
                    if len(r) != ncols:
                        raise ValueError(f"{table}: expected {ncols} columns, got {len(r)}: {r!r}")
                    gz.write(("\t".join(clean_cell(v) for v in r) + "\n").encode("utf-8"))
                    n += 1
    except BaseException:
        os.remove(tmp)
        raise
    os.replace(tmp, path)
    return n


# --- loading into SQLite --------------------------------------------------------
def _signature(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_table(conn, table, path, sig):
    spec = TABLES[table]
    cols = [_col(c) for c in spec["cols"]]
    names = [c for c, _ in cols]
    keys = list(spec["keys"].items())
    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(f'{c} {t}' for c, t in cols)}, "
                 f"{', '.join(f'{k} TEXT' for k, _ in keys)})")
    types = [t for _, t in cols]
    src_idx = [names.index(s) for _, s in keys]
    sql = f"INSERT INTO {table} VALUES ({', '.join('?' * (len(names) + len(keys)))})"

    def rows():
        with gzip.open(path, "rt", encoding="utf-8") as f:
            header = f.readline().rstrip("\n").split("\t")
            # a file from before columns were added at the end loads with them blank
            if header != names[:len(header)]:
                raise ValueError(f"{path}: header {header} != {names}")
            pad = [""] * (len(names) - len(header))
            for line in f:
                cells = line.rstrip("\n").split("\t") + pad
                out = []
                for v, t in zip(cells, types):
                    if t == "INTEGER":
                        out.append(int(v) if v else None)
                    elif t == "REAL":
                        out.append(float(v) if v else None)
                    else:
                        out.append(v)
                yield out + [canon_key(cells[i]) for i in src_idx]

    n = 0
    batch = []
    for r in rows():
        batch.append(r)
        if len(batch) >= 20000:
            conn.executemany(sql, batch)
            n += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    for i, idx in enumerate(spec["indexes"]):
        conn.execute(f"CREATE INDEX {table}_i{i} ON {table} ({', '.join(idx)})")
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?, ?, ?)", (table, sig, n, time.time()))
    conn.commit()
    return n


class DetailStore:
    """The loaded tables. load() is safe to call from a background thread while
    queries run: a table is queried only once its load has committed."""

    def __init__(self, src_dir, db_path):
        self.src_dir = str(src_dir)
        self.db_path = str(db_path)
        self.ready = {}        # table -> row count, for loaded tables
        self.errors = {}       # table -> error text
        self.loading = False
        self._local = threading.local()
        self._lock = threading.Lock()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            c.row_factory = sqlite3.Row
            self._local.conn = c
        return c

    def load(self, log=print):
        """(Re)load every table whose TSV changed since the last load. Tables
        already loaded from the same file are kept (a restart costs nothing)."""
        with self._lock:
            self.loading = True
            try:
                os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
                conn = sqlite3.connect(self.db_path)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=OFF")
                conn.execute("CREATE TABLE IF NOT EXISTS meta (tbl TEXT PRIMARY KEY, sig TEXT, rows INTEGER, loaded REAL)")
                have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT tbl, sig, rows FROM meta")}
                for table in TABLES:
                    path = os.path.join(self.src_dir, tsv_name(table))
                    if not os.path.exists(path):
                        continue
                    try:
                        # the column spec is part of it: new columns reload an unchanged file
                        sig = _signature(path) + ":" + hashlib.sha1(
                            json.dumps(TABLES[table], sort_keys=True).encode()).hexdigest()[:12]
                        if have.get(table, (None,))[0] == sig:
                            self.ready[table] = have[table][1]
                            continue
                        self.ready.pop(table, None)
                        t0 = time.time()
                        n = _load_table(conn, table, path, sig)
                        self.ready[table] = n
                        self.errors.pop(table, None)
                        log(f"detail table {table}: {n:,} rows in {time.time() - t0:.0f}s")
                    except Exception as e:  # one bad file must not hide the others
                        self.errors[table] = f"{type(e).__name__}: {e}"
                        log(f"detail table {table} failed: {e}")
                conn.close()
            finally:
                self.loading = False
        self._local = threading.local()   # reopen read connections on the new data

    def status(self):
        try:
            size = os.path.getsize(self.db_path)
        except OSError:
            size = None
        return {"ready": dict(self.ready), "errors": dict(self.errors), "loading": self.loading,
                "db_mb": round(size / 1e6) if size else None}

    def has(self, table):
        return table in self.ready

    def q(self, table, sql, args=()):
        if table not in self.ready:
            return None
        return [dict(r) for r in self._conn().execute(sql, args).fetchall()]

    # --- lookups ----------------------------------------------------------------
    # Every lookup takes a list of name keys (canon_key of the entity's name and
    # its aliases) and returns plain dicts. None means the table isn't loaded.

    def committees_for(self, keys):
        """Committees named by keys, or belonging to the candidate/sponsor named by keys."""
        ph = _ph(keys)
        rows = self.q("committees", f"SELECT DISTINCT cmte_id, committee, linked, role FROM committees "
                                    f"WHERE committee_key IN ({ph}) OR linked_key IN ({ph})", keys + keys)
        found = {r["cmte_id"]: r for r in rows or []}
        tot = self.q("cmte_totals", f"SELECT cmte_id, committee FROM cmte_totals WHERE committee_key IN ({ph})", keys)
        for r in tot or []:
            found.setdefault(r["cmte_id"], {"cmte_id": r["cmte_id"], "committee": r["committee"],
                                            "linked": "", "role": ""})
        return list(found.values())

    def top_donors(self, keys, limit=25):
        """Biggest donors to the committees of keys (a committee, or a candidate's committees)."""
        cmtes = self.committees_for(keys)
        if not cmtes or not self.has("donors"):
            return {"committees": cmtes, "donors": [], "total_donors": 0, "total_usd": 0.0}
        ids = [c["cmte_id"] for c in cmtes]
        ph = _ph(ids)
        donors = self.q("donors", f"""
            SELECT donor, donor_type, MAX(employer) AS employer, MAX(occupation) AS occupation,
                   MAX(state) AS state, SUM(usd) AS usd, SUM(n) AS n,
                   GROUP_CONCAT(DISTINCT committee) AS committees, GROUP_CONCAT(cycles) AS cycles
            FROM donors WHERE cmte_id IN ({ph}) GROUP BY donor_key, donor_type
            ORDER BY usd DESC LIMIT ?""", ids + [limit])
        for d in donors:
            d["cycles"] = _merge_cycles(d["cycles"])
        tot = self.q("cmte_totals", f"SELECT SUM(donors) AS donors, SUM(usd) AS usd FROM cmte_totals "
                                    f"WHERE cmte_id IN ({ph})", ids) or [{}]
        return {"committees": cmtes, "donors": donors,
                "total_donors": tot[0].get("donors") or 0, "total_usd": tot[0].get("usd") or 0.0}

    def donations_by(self, keys, limit=50):
        """Where a donor's money went, biggest first, with the candidate each committee belongs to."""
        ph = _ph(keys)
        rows = self.q("donors", f"""
            SELECT d.cmte_id, d.committee, SUM(d.usd) AS usd, SUM(d.n) AS n,
                   GROUP_CONCAT(d.cycles) AS cycles, MAX(d.employer) AS employer,
                   MAX(d.occupation) AS occupation, MAX(d.state) AS state, MIN(d.donor_type) AS donor_type,
                   GROUP_CONCAT(DISTINCT d.donor) AS recorded_as
            FROM donors d WHERE d.donor_key IN ({ph}) GROUP BY d.cmte_id
            ORDER BY usd DESC LIMIT ?""", keys + [limit])
        if rows is None:
            return None
        linked = {}
        if rows and self.has("committees"):
            ids = [r["cmte_id"] for r in rows]
            for c in self.q("committees", f"SELECT cmte_id, linked, role FROM committees "
                                          f"WHERE cmte_id IN ({_ph(ids)}) AND role IN ('candidate', 'leadership_pac')", ids):
                linked.setdefault(c["cmte_id"], c["linked"])
        for r in rows:
            r["cycles"] = _merge_cycles(r["cycles"])
            r["candidate"] = linked.get(r["cmte_id"], "")
        return rows

    def pac_flows(self, keys, limit=25):
        """Money a committee gave (out) and received from other committees (in)."""
        ph = _ph(keys)
        out = self.q("pac_flows", f"SELECT recipient AS other, kind, usd, n, cycles FROM pac_flows "
                                  f"WHERE giver_key IN ({ph}) ORDER BY usd DESC LIMIT ?", keys + [limit])
        inn = self.q("pac_flows", f"SELECT giver AS other, kind, usd, n, cycles FROM pac_flows "
                                  f"WHERE recipient_key IN ({ph}) ORDER BY usd DESC LIMIT ?", keys + [limit])
        if out is None:
            return None
        return {"gave": out, "received": inn}

    def vendors(self, keys, limit=25):
        """Who a committee paid, and (for a vendor) which committees paid it."""
        ph = _ph(keys)
        paid = self.q("vendors", f"SELECT payee AS other, usd, n, cycles, purpose FROM vendors "
                                 f"WHERE payer_key IN ({ph}) ORDER BY usd DESC LIMIT ?", keys + [limit])
        paid_by = self.q("vendors", f"SELECT payer AS other, usd, n, cycles, purpose FROM vendors "
                                    f"WHERE payee_key IN ({ph}) ORDER BY usd DESC LIMIT ?", keys + [limit])
        if paid is None:
            return None
        return {"paid": paid, "paid_by": paid_by}

    _DATED_SOURCE = "SEC_FORM_345"   # first/last filing dates: says current or former
    _LEAD_RAW_CAP = 50000
    _STATUS_ORDER = {"current": 0, "listed as current": 1, "": 2}

    @classmethod
    def _person_merge_key(cls, k):
        """'mary t barra' and 'mary barra' are one row within one organization."""
        toks = [t for t in k.split() if len(t) > 1 and t not in cls._NAME_SUFFIXES]
        return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else k

    @classmethod
    def _org_merge_key(cls, k):
        toks = k.split()
        if toks and toks[0] == "the":
            toks = toks[1:]
        while len(toks) > 1 and toks[-1] in cls._ORG_SUFFIXES:
            toks.pop()
        return " ".join(toks) or k

    def _lead(self, where_col, keys, name_col, merge_key, limit):
        """Leadership rows for keys, one per person (or organization), 2026-10-20:
        all titles and sources, the years held where known, and a status --
        SEC filings decide current/former where there are any (a director files
        at least yearly), otherwise the source's own wording."""
        raw = self.q("leadership", f"SELECT org, person, org_key, person_key, title, source, year, since, until, "
                                   f"current FROM leadership WHERE {where_col} IN ({_ph(keys)}) LIMIT ?",
                     keys + [self._LEAD_RAW_CAP])
        if raw is None:
            return None
        key_col = "person_key" if name_col == "person" else "org_key"
        groups = {}
        for r in raw:
            g = groups.setdefault(merge_key(r[key_col]), {"names": [], "titles": [], "sources": [], "year": "",
                                                          "since": "", "until": "", "sec": None, "yes": False,
                                                          "no": False})
            for lst, v in ((g["names"], r[name_col]), (g["titles"], r["title"]), (g["sources"], r["source"])):
                if v and v not in lst:
                    lst.append(v)
            g["year"] = max(g["year"], r["year"] or "")
            if r["since"]:
                g["since"] = min(g["since"] or r["since"], r["since"])
            g["until"] = max(g["until"], r["until"] or "")
            if r["source"] == self._DATED_SOURCE:
                cur, last = g["sec"] or (False, "")
                g["sec"] = (cur or r["current"] == "yes", max(last, r["until"] or ""))
            elif r["current"] == "yes":
                g["yes"] = True
            elif r["current"] == "no":
                g["no"] = True
        out = []
        for g in groups.values():
            s, u = g["since"], g["until"]
            years = (f"{s}–{u}" if s != u else s) if s and u else f"{s}–" if s else f"–{u}" if u else g["year"]
            if g["sec"]:
                status = "current" if g["sec"][0] else f"former (last SEC filing {g['sec'][1]})"
            else:
                status = "former" if g["no"] else "listed as current" if g["yes"] else ""
            names = sorted(g["names"], key=lambda n: (self._case_rank(n), -len(n)))
            row = {name_col: names[0], "titles": ",".join(g["titles"]), "years": years, "status": status,
                   "sources": ",".join(g["sources"])}
            if len(names) > 1:
                row["recorded_as"] = ",".join(names)
            out.append(row)
        # current first, then specific titles (LittleSis's bare "Position" last),
        # rows confirmed by more sources, the latest year
        out.sort(key=lambda r: (self._STATUS_ORDER.get(r["status"], 3),
                                all(t == "Position" for t in r["titles"].split(",")),
                                -len(r["sources"].split(",")),
                                -max(map(int, re.findall(r"\d{4}", r["years"])), default=0), r[name_col]))
        return out[:limit]

    def leadership_of(self, keys, limit=200):
        """People with a role at an organization: one row per person."""
        return self._lead("org_key", keys, "person", self._person_merge_key, limit)

    def roles_of(self, keys, limit=200):
        """A person's roles at organizations: one row per organization."""
        return self._lead("person_key", keys, "org", self._org_merge_key, limit)

    _TRADE_COLS = ("year, kind, SUM(n) AS trades, SUM(shares) AS shares, SUM(usd) AS usd, MAX(max_usd) AS largest, "
                   "MAX(last) AS last, MAX(joint) AS joint, SUM(unpriced) AS unpriced")

    def insider_trades(self, keys, limit=100):
        """SEC-reported stock trades (2026-10-20): a company's insiders
        ('at_company', open-market buys and sales first) and a person's own
        trades ('by_person'), per year and kind of trade."""
        ph = _ph(keys)
        at = self.q("insider_trades", f"""
            SELECT insider, company, {self._TRADE_COLS} FROM insider_trades WHERE company_key IN ({ph})
            GROUP BY insider_key, year, code ORDER BY code NOT IN ('P', 'S'), year DESC, usd DESC LIMIT ?""",
                    keys + [limit])
        if at is None:
            return None
        by = self.q("insider_trades", f"""
            SELECT company, {self._TRADE_COLS} FROM insider_trades WHERE insider_key IN ({ph})
            GROUP BY company_key, year, code ORDER BY code NOT IN ('P', 'S'), year DESC, usd DESC LIMIT ?""",
                    keys + [limit])
        for r in at + by:
            r["shares"] = round(r["shares"] or 0)
            r["joint"] = "yes" if r["joint"] else ""
            r["unpriced"] = r["unpriced"] or ""
        return {"at_company": at, "by_person": by}

    def lobbying(self, keys, limit=200):
        """Lobbying where keys is the client, the registrant firm or the lobbyist."""
        ph = _ph(keys)
        res = {}
        for role, col in (("as_client", "client_key"), ("as_registrant", "registrant_key"),
                          ("as_lobbyist", "lobbyist_key")):
            rows = self.q("lobbying", f"""
                SELECT lobbyist, client, registrant, MAX(year) AS year, GROUP_CONCAT(DISTINCT issue) AS issues,
                       MAX(covered_position) AS covered_position
                FROM lobbying WHERE {col} IN ({ph}) GROUP BY lobbyist_key, client_key, registrant_key
                ORDER BY MAX(year) DESC, lobbyist LIMIT ?""", keys + [limit])
            if rows is None:
                return None
            res[role] = rows
        return res

    def person_variants(self, keys, limit=40):
        """Other spellings of a person's name that the person-keyed tables hold:
        the formal form of a nickname (Ken -> Kenneth) and a single middle
        initial ("kenneth c griffin"), 2026-10-20. FEC alone spells Ken Griffin
        three ways, and the graph keeps them as separate nodes without aliases.
        A namesake can be merged in this way, so the lookups say which
        spellings each row came from (recorded_as)."""
        bases = []
        for k in keys:
            toks = k.split()
            if len(toks) == 3 and len(toks[1]) == 1:
                toks = [toks[0], toks[2]]
            if len(toks) == 2:
                b = " ".join(toks)
                for f in [b] + formal_forms(b):
                    if f not in bases:
                        bases.append(f)
        out = []
        for b in bases:
            if b not in keys and b not in out:
                out.append(b)
            first, last = b.split()
            for table, col in (("donors", "donor_key"), ("leadership", "person_key"), ("lobbying", "lobbyist_key")):
                rows = self.q(table, f"SELECT DISTINCT {col} AS k FROM {table} WHERE {col} >= ? AND {col} < ? "
                                     f"AND {col} LIKE ? LIMIT ?", (first + " ", first + " \U0010ffff",
                                                                   f"{first} _ {last}", limit))
                for r in rows or []:
                    if r["k"] not in keys and r["k"] not in out:
                        out.append(r["k"])
        return out[:limit]

    _NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

    def candidate_matches(self, keys, limit=6):
        """FEC candidates filed under a fuller form of a person's name: 'ken
        paxton' (with its formal form 'kenneth paxton') -> 'Warren Kenneth
        Paxton Jr', 2026-10-20. Only first-and-last keys are tried; the
        candidate's last name (before any suffix) must match and the first name
        must appear before it."""
        pairs = []
        for k in keys:
            toks = k.split()
            if len(toks) == 2 and (toks[0], toks[1]) not in pairs:
                pairs.append((toks[0], toks[1]))
        out = []
        for first, last in pairs:
            rows = self.q("committees", "SELECT DISTINCT linked, linked_key FROM committees "
                                        "WHERE role = 'candidate' AND ' ' || linked_key || ' ' LIKE ? "
                                        "AND ' ' || linked_key || ' ' LIKE ?", (f"% {first} %", f"% {last} %"))
            for r in rows or []:
                toks = r["linked_key"].split()
                while len(toks) > 2 and toks[-1] in self._NAME_SUFFIXES:
                    toks.pop()
                if (toks[-1] == last and first in toks[:-1] and r["linked_key"] not in keys
                        and r["linked"] not in out):
                    out.append(r["linked"])
        return out[:limit]

    _ORG_SUFFIXES = ("co", "company", "corp", "corporation", "inc", "incorporated", "ltd", "llc", "plc")

    def org_variants(self, keys):
        """The same organization with or without a corporate suffix: 'general
        motors' / 'general motors co' / 'general motors company' (LittleSis
        and Form 4 spell GM differently), 2026-10-20."""
        out = []
        for k in keys:
            toks = k.split()
            if toks and toks[0] == "the":
                toks = toks[1:]
            while len(toks) > 1 and toks[-1] in self._ORG_SUFFIXES:
                toks.pop()
            base = " ".join(toks)
            if len(base) < 4:
                continue
            for v in [base] + [f"{base} {s}" for s in self._ORG_SUFFIXES]:
                if v not in keys and v not in out:
                    out.append(v)
        return out

    @staticmethod
    def _case_rank(name):
        """Prefer 'Ken Paxton' over 'KEN PAXTON' over 'ken paxton' for display."""
        return 2 if name.islower() else 1 if name.isupper() else 0

    def suggest(self, prefix_key, limit=10):
        """Names in any table whose key starts with prefix_key (for "did you
        mean"), one per name: tables spell the same name in different letter
        cases ('KEN PAXTON', 'Ken Paxton'), 2026-10-20."""
        if len(prefix_key) < 3:
            return []
        hi = prefix_key + "\U0010ffff"
        best = {}   # canon_key -> display name, in first-seen order
        for table, cols in (("cmte_totals", [("committee_key", "committee")]),
                            ("committees", [("linked_key", "linked")]),
                            ("leadership", [("org_key", "org"), ("person_key", "person")]),
                            ("lobbying", [("client_key", "client"), ("registrant_key", "registrant")]),
                            ("pac_flows", [("giver_key", "giver")]),
                            ("vendors", [("payee_key", "payee")]),
                            ("donors", [("donor_key", "donor")])):
            for key_col, name_col in cols:
                rows = self.q(table, f"SELECT DISTINCT {name_col} AS name, {key_col} AS k FROM {table} "
                                     f"WHERE {key_col} >= ? AND {key_col} < ? LIMIT ?", (prefix_key, hi, limit * 3))
                for r in rows or []:
                    if not r["name"]:
                        continue
                    cur = best.get(r["k"])
                    if cur is None:
                        if len(best) < limit:
                            best[r["k"]] = r["name"]
                    elif self._case_rank(r["name"]) < self._case_rank(cur):
                        best[r["k"]] = r["name"]
        return list(best.values())


def _ph(items):
    return ", ".join("?" * max(len(items), 1))


def _merge_cycles(s):
    """'2008,2012,2008' (from GROUP_CONCAT of cycle lists) -> '2008, 2012'."""
    ys = sorted({c for c in (s or "").replace(" ", "").split(",") if c})
    return ", ".join(ys)
