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
import os
import re
import sqlite3
import threading
import time
import unicodedata

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
        "cols": ["org", "person", "title", "source", "year"],
        "keys": {"org_key": "org", "person_key": "person"},
        "indexes": [["org_key"], ["person_key"]],
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
            if header != names:
                raise ValueError(f"{path}: header {header} != {names}")
            for line in f:
                cells = line.rstrip("\n").split("\t")
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
                        sig = _signature(path)
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
                   MAX(d.occupation) AS occupation, MAX(d.state) AS state, MIN(d.donor_type) AS donor_type
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

    # specific titles first (LittleSis's bare "Position" last), then rows confirmed
    # by more sources, then the latest year
    _LEAD_ORDER = ("MAX(title <> 'Position') DESC, COUNT(DISTINCT source) DESC, "
                   "(MAX(year) IS NULL OR MAX(year) = ''), MAX(year) DESC")

    def leadership_of(self, keys, limit=200):
        """People with a role at an organization: one row per person, all titles and sources."""
        rows = self.q("leadership", f"""
            SELECT person, GROUP_CONCAT(DISTINCT title) AS titles, GROUP_CONCAT(DISTINCT source) AS sources,
                   MAX(year) AS year
            FROM leadership WHERE org_key IN ({_ph(keys)}) GROUP BY person_key
            ORDER BY {self._LEAD_ORDER}, person LIMIT ?""", keys + [limit])
        return rows

    def roles_of(self, keys, limit=200):
        """A person's roles at organizations."""
        return self.q("leadership", f"""
            SELECT org, GROUP_CONCAT(DISTINCT title) AS titles, GROUP_CONCAT(DISTINCT source) AS sources,
                   MAX(year) AS year
            FROM leadership WHERE person_key IN ({_ph(keys)}) GROUP BY org_key
            ORDER BY {self._LEAD_ORDER}, org LIMIT ?""", keys + [limit])

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

    def suggest(self, prefix_key, limit=10):
        """Names in any table whose key starts with prefix_key (for "did you mean")."""
        if len(prefix_key) < 3:
            return []
        hi = prefix_key + "\U0010ffff"
        out = []
        for table, cols in (("cmte_totals", [("committee_key", "committee")]),
                            ("committees", [("linked_key", "linked")]),
                            ("leadership", [("org_key", "org"), ("person_key", "person")]),
                            ("lobbying", [("client_key", "client"), ("registrant_key", "registrant")]),
                            ("pac_flows", [("giver_key", "giver")]),
                            ("vendors", [("payee_key", "payee")]),
                            ("donors", [("donor_key", "donor")])):
            for key_col, name_col in cols:
                rows = self.q(table, f"SELECT DISTINCT {name_col} AS name FROM {table} "
                                     f"WHERE {key_col} >= ? AND {key_col} < ? LIMIT ?", (prefix_key, hi, limit))
                for r in rows or []:
                    if r["name"] and r["name"] not in out:
                        out.append(r["name"])
                if len(out) >= limit:
                    return out[:limit]
        return out


def _ph(items):
    return ", ".join("?" * max(len(items), 1))


def _merge_cycles(s):
    """'2008,2012,2008' (from GROUP_CONCAT of cycle lists) -> '2008, 2012'."""
    ys = sorted({c for c in (s or "").replace(" ", "").split(",") if c})
    return ", ".join(ys)
