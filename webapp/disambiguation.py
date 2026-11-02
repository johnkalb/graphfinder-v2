"""Split common-name collisions in FEC donors and patent inventors (2026-10-02).

Every "MICHAEL SMITH" in FEC donation records, and every "Wei Wang" on a
patent, used to be one graph node: tens of thousands of unrelated people
fused into hubs that formed fake communities (74K inventors, 61K donors).
The records carry what tells them apart -- a donation's employer, a patent's
assignee company -- so for AMBIGUOUS names only, those edges move to
per-employer / per-company nodes: "MICHAEL SMITH (Cornerstone Government
Affairs)", "Wei Wang (Huawei Technologies)".

A name is ambiguous when its records show at least MIN_DISTINCT different
employers (or assignees). Measured 2026-10-02: about 10K donor names and
5.5K inventor names. Below that, job changes of one real person
("Greylock Partners", "LinkedIn") would split them. Verified public figures
are never split. Donations of an ambiguous name with no usable employer
("RETIRED", "NOT EMPLOYED") and patents with no assignee can't be attributed
to anyone, so they're dropped (they're weak links anyway).

Used by build_scored_edges.py, and also copied to optiplex with it.
"""
import json
import unicodedata
import re

MIN_DISTINCT = 4
# Verified public figures aren't split -- unless the name spans this many
# employers/companies, which no one person does (Buffett 1, Reid Hoffman 3).
EXEMPT_MAX = 10
_SUFFIX = re.compile(r"\b(inc|llc|llp|lp|ltd|corp|corporation|co|company|pc|pllc|the)\b")
UNINFORMATIVE = {"", "none", "na", "n a", "retired", "self", "self employed", "selfemployed", "not employed",
                 "unemployed", "homemaker", "information requested", "information requested per best efforts",
                 "requested", "student", "null", "not applicable", "disabled", "unknown"}


def org_norm(name):
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", s)).strip()


def _display_org(name):
    name = re.sub(r"\s+", " ", (name or "").strip(" ,."))
    return name.title() if name.isupper() else name


def _evidence(ev):
    if not ev:
        return {}
    try:
        d = json.loads(ev)
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


class Splitter:
    """Two phases: feed every FEC donation and patent INVENTOR_AT row
    (observe_*), call finalize(), then map each edge endpoint (fec_endpoint,
    patent_endpoint). Keys are the build's canon keys; the caller supplies
    exempt keys (verified public figures)."""

    def __init__(self, exempt_keys=()):
        self.exempt = set(exempt_keys)
        self._fec_emps = {}           # name key -> set of informative employers
        self._inv_orgs = {}           # name key -> set of assignees
        self.patent_assignee = {}     # patent_id -> assignee display name (first seen)
        self.fec_ambiguous = set()
        self.inv_ambiguous = set()
        self.dropped = 0
        self.split = 0

    # --- phase 1 ---
    def observe_fec(self, key, evidence):
        emp = org_norm(_evidence(evidence).get("employer"))
        if emp not in UNINFORMATIVE:
            self._fec_emps.setdefault(key, set()).add(emp)

    def observe_inventor_at(self, key, assignee, evidence):
        self._inv_orgs.setdefault(key, set()).add(org_norm(assignee))
        pid = _evidence(evidence).get("patent_id")
        if pid and pid not in self.patent_assignee:
            self.patent_assignee[pid] = assignee

    def finalize(self):
        def ambiguous(k, v):
            # A verified name is exempt only while its record spread looks
            # like one person: "MICHAEL SMITH" and "Wei Wang" are verified
            # (a journalist, a scientist) yet show 100+ employers/companies.
            return len(v) >= MIN_DISTINCT and (k not in self.exempt or len(v) >= EXEMPT_MAX)
        self.fec_ambiguous = {k for k, v in self._fec_emps.items() if ambiguous(k, v)}
        self.inv_ambiguous = {k for k, v in self._inv_orgs.items() if ambiguous(k, v)}
        self._fec_emps = self._inv_orgs = None

    # --- phase 2: returns (key, display), or None to drop the edge ---
    def _split(self, key, display, org):
        self.split += 1
        return f"{key} ({org_norm(org)})", f"{display} ({_display_org(org)})"

    def fec_endpoint(self, key, display, evidence):
        if key not in self.fec_ambiguous:
            return key, display
        emp = _evidence(evidence).get("employer")
        if org_norm(emp) in UNINFORMATIVE:
            self.dropped += 1
            return None
        return self._split(key, display, emp)

    def patent_endpoint(self, key, display, evidence, assignee=None):
        """assignee: the INVENTOR_AT target, or None for a co-inventor edge
        (then the patent's assignee is looked up by patent_id)."""
        if key not in self.inv_ambiguous:
            return key, display
        org = assignee or self.patent_assignee.get(_evidence(evidence).get("patent_id"))
        if not org:
            self.dropped += 1
            return None
        return self._split(key, display, org)


# Placeholder names that sources wrote where a name belongs. They became
# hubs ("." had 111 links, "None None" 24, "SEE SCH O FOR COMPENSATION" 21)
# that tie unrelated records together. Whole-name matches only -- "Na" is a
# real given name ("Na Li"), and "None But the Brave (1965 film)" is a film.
_JUNK = re.compile(
    r"[\W_]*"
    r"|(?:none|null|nil|vacant|unknown|tbd|n/?a|various|anonymous|not applicable|name withheld"
    r"|information requested|same as above)(?: (?:none|vacant|various))?"
    r"|none \d+%"
    r"|see (?:sch|schedule|supplemental|attached|statement|part)\b.*",
    re.I)


def is_junk_name(name):
    return bool(_JUNK.fullmatch((name or "").strip()))


# FEC donations were stored with placeholder targets ("FEC Campaign Committee
# C0040122" -- the committee id, truncated to 8 characters, so up to ten
# committees shared one node). The full id is in each row's evidence
# ("recipient"); the committee name comes from FEC's committee master files
# (data/fec_committee_names.json.gz, built from cmYY.zip). ActBlue and WinRed
# are conduits that pass money on to candidates: a donation recorded only "to
# ActBlue" doesn't say who got it, so it's dropped (about 55K of 760K rows,
# measured 2026-10-02).
FEC_PLACEHOLDER = "FEC Campaign Committee"
FEC_CONDUITS = {"C00401224", "C00694323"}     # ACTBLUE, WINRED


def fec_recipient(evidence, committee_names):
    """The committee name for a placeholder FEC target, the placeholder with
    the FULL id if the name is unknown, or None to drop the edge."""
    rid = (_evidence(evidence).get("recipient") or "").strip()
    if not rid:
        return None
    if rid in FEC_CONDUITS:
        return None
    return committee_names.get(rid) or f"{FEC_PLACEHOLDER} {rid}"


# --- Union officers vs. elite namesakes (2026-11-01) -----------------------
# "Michael Grunwald" was one node: an Overbrook Foundation trustee, a
# Washington Post journalist, an AFGE Local 2040 vice president ($599/yr) and
# an IBEW international rep -- so paths ran from a foundation board straight
# into the unions. Union officer filings (OLMS LM-2, ~488K rows, mostly
# locals) are the likeliest source of such merges. For a name that
#   * holds LM-2 union roles, AND
#   * also has ELITE ties (corporate insider, LittleSis, Wikidata, law firm,
#     judge, investment adviser) to non-union organizations, AND
#   * is shared by 2+ different people (2+ FEC donor regions, 2026 donor file),
# the union roles and union-related organizations (locals, union funds,
# apprenticeship trusts...) move to a per-union node "Name (Union)". Known
# union leaders -- a national union role AND a LittleSis/Wikidata record of a
# union role -- stay merged. Measured on the DB 2026-11-01: 14,903 names split, ~76%
# estimated correct (rare-name baseline); the broader rule (any non-union
# board) was only ~23% correct, because local officers really do sit on food
# bank, Boys & Girls Club and chamber boards.
UNION_RE = re.compile(
    r"\b(local|union|afl[- ]?cio|apprentic|joint (board|council)|health (and|&) welfare|pension|"
    r"annuity|benefit (fund|trust|plan)|trust fund|training (fund|trust|center)|workers|brotherhood|"
    r"teamsters|laborers|steelworkers|machinists|carpenters|electricians|plumbers|pipefitters|"
    r"ironworkers|iron workers|seiu|afscme|ibew|uaw|ufcw|liuna|iatse|nurses association|"
    r"education association|federation of teachers|ind|teachers|federation of|guild|players association|"
    r"craftsmen|employees|building service|32 ?b-?j|wkrs|labor|letter carriers|firefighters|fire fighters|"
    r"police|nurses|sheet metal|electrical|musicians|actors|operating engineers|painters|bricklayers|"
    r"roofers|boilermakers|insulators|glaziers|elevator|sprinkler|stagehands|transit|transport|aft|nea|"
    r"cwa|unite here|seafarers|longshore|pilots|flight attendants|carriers|postal|retirees|scholarship)\b", re.I)
UNION_ELITE_SOURCES = ("LITTLESIS", "SEC_FORM_345", "SEC_FORM_4", "WIKIDATA", "WIKIDATA_MEDIA",
                       "WIKIDATA_INFLUENCERS", "AMLAW_ROSTER", "CL_JUDGES", "SEC_ADV", "SEC_ADV_OWNERS")
UNION_KNOWN_SOURCES = {"LITTLESIS", "WIKIDATA", "WIKIDATA_MEDIA", "WIKIDATA_INFLUENCERS"}
UNION_SKIP_RELATIONS = ("MENTIONED_WITH", "DONATION", "TRANSACTION", "LOBBYING", "PAID_BY")
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "md", "phd", "esq", "dr", "mr", "mrs", "ms"}
_NATIONAL = ("NHQ", "NAT", "INTL")


def first_last(name):
    """'Michael R. Grunwald Jr' -> 'michael grunwald' (the FEC region-table key)."""
    s = re.sub(r"[^a-z ]", " ", unicodedata.normalize("NFKD", (name or "").lower()))
    t = [w for w in s.split() if w not in _NAME_SUFFIXES]
    return f"{t[0]} {t[-1]}" if len(t) >= 2 else ""


class UnionSplitter:
    """Two phases, like Splitter: observe_* in a pre-pass, finalize(), then
    endpoint() in the main loop for each row of a split name."""

    def __init__(self, name_regions):
        self.name_regions = name_regions or {}    # first+last -> FEC donor ZIP3 regions
        self._unions = {}                         # key -> Counter(union -> rows)
        self._national = set()
        self._elite, self._known = set(), set()
        self._display = {}
        self.split_names = set()
        self.primary = {}
        self.moved = 0

    def observe_lm2(self, key, display, union, evidence):
        from collections import Counter
        self._unions.setdefault(key, Counter())[union] += 1
        self._display.setdefault(key, display)
        if (_evidence(evidence).get("designation") or "").strip().startswith(_NATIONAL):
            self._national.add(key)

    def has_union_roles(self, key):
        return key in self._unions

    def observe_other(self, key, target, source):
        """An elite-source row of a union-filing name (call only for those keys)."""
        # a union leader is one whose LittleSis/Wikidata record is itself a union
        # role -- Michael Grunwald's Wikidata record is the Post journalist's
        if source in UNION_KNOWN_SOURCES and target and UNION_RE.search(target):
            self._known.add(key)
        if (target and not UNION_RE.search(target) and not re.search(r"\d{5,}|\(\d{4} (film|work)\)", target)
                and target.lower() != self._display.get(key, "").lower()):
            self._elite.add(key)

    def finalize(self):
        for key in self._elite:
            if key in self._national and key in self._known:
                continue                           # a known union leader: one person
            if self.name_regions.get(first_last(self._display.get(key, key)), 0) >= 2:
                self.split_names.add(key)
                self.primary[key] = self._unions[key].most_common(1)[0][0]
        self._unions = self._elite = self._known = self._national = None

    def wants(self, key, source, target):
        return key in self.split_names and (source == "OLMS_LM2" or bool(UNION_RE.search(target or "")))

    def endpoint(self, key, display, source, target):
        """(key, display) of the per-union node for this row."""
        union = target if source == "OLMS_LM2" else self.primary[key]
        self.moved += 1
        return f"{key} ({org_norm(union)})", f"{display} ({_display_org(union)})"
