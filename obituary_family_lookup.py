#!/usr/bin/env python3
"""Dignity Memorial obituary lookup: on-demand family + board-membership
extraction for a KNOWN name, not a bulk crawler.

Why not a Kenya-style blind crawl: ripkenya.co.ke (the Kenya project's
source) is ~1,800 posts total -- small enough to crawl in full. Dignity
Memorial's obituary corpus is orders of magnitude larger and the overwhelming
majority of it has nothing to do with sixdegrees's ~847K-node graph (SEC
filers, LittleSis subjects, IRS-990 nonprofit officers, politicians). So
this looks up ONE name at a time via Dignity Memorial's own
/obituaries/name/{first}/{last} disambiguation page -- a real, crawlable
page per robots.txt, distinct from the disallowed `?keyword=` search
endpoint -- and only proceeds when the result is unambiguous (exactly one
candidate). A common name with multiple candidates is skipped, not guessed
at, mirroring the confidence-gate pattern in webapp/pathfinder.py's
_qa_find_person().

robots.txt checked live 2026-09-03: individual obituary pages and the
/obituaries/name/{first}/{last} listing pages are NOT disallowed; only
`/obituaries?keyword=*` (site search) and unrelated functional paths
(checkout, forms) are. This script never touches the disallowed endpoint.

Source structure: both the listing page and the individual obituary page
embed their data as clean JSON in a `__NEXT_DATA__` script tag (Next.js) --
no fragile HTML div-scraping needed, unlike the Kenya project's WordPress
target. See props.pageProps.obitsInit.results (listing) and
props.pageProps.obituary.{Obituary,Subject} (individual page).

Extraction:
- FAMILY relations: ports the Kenya project's approach
  (kenya/src/kenya_network/sources/newspaper_families.py) to US obituary
  grammar. Kenyan obituaries say "son OF X" (label + "of" + name); US ones
  say "his son, X" (possessive + label + comma + name) -- different enough
  to need a new parser, not a straight port. Handles "survived by" /
  "preceded in death by" (or "predeceased by") clauses.
- ORGANIZATION relations: NEW capability -- the Kenya project doesn't
  extract organizations from obituary text at all (it gets orgs from
  separate sources). Board-membership phrasing ("served ... on the board
  of directors for/of X") is the one pattern validated solid against real
  data; it's also the single highest-value pattern since it can overlap
  directly with SEC-sourced board data already in the graph. A looser
  title+org pattern ("Mine Manager at X", "CEO of X", ...) is best-effort
  and only captures the FIRST org when a sentence lists several
  ("Mine Manager at Mattabi, Geco and Hemlo Mines" -> catches "Mattabi"
  only) -- a known, documented simplification, not a bug to chase yet.

First slice covers ONLY dignitymemorial.com. echovita.com was also checked
during the investigation and looked viable as a second source (similarly
permissive robots.txt) but isn't wired up here -- a natural follow-up once
this slice is validated against real production names.

Environment note: on this machine, `requests` fails all HTTPS calls with
SSLCertVerificationError against real sites, including this one -- Norton
Antivirus does TLS interception (Norton Web/Mail Shield) and presents its
own locally-generated root cert, which is in the Windows OS trust store
(so curl/browsers work fine via schannel) but NOT in Python's bundled
certifi CA file. Fixed once, machine-wide, by installing `pip_system_certs`
(patches Python's SSL to use the Windows cert store) -- confirmed present
2026-09-03. If this script starts throwing SSL errors again, check that
package is still installed before suspecting the target site.

Validated 2026-09-03 against two real, structurally different obituaries
(clean/simple: John Keyes; messy/real-world: Peter Bemis -- 5 separate
"survived by" sentences across paragraphs, step-family, in-laws, a former
spouse) -- 23/23 family relationships and all but one org affiliation
extracted correctly on the harder sample. Several real bugs were found
and fixed via this process, not just assumed away -- see inline comments
at _PRECEDED_RE/_SURVIVED_RE (multi-sentence clauses), _BOARD_RE (a stray
re.IGNORECASE on the whole pattern silently made the "capitalized words
only" bound match lowercase too), and _clean_extracted_name (trailing
"of <city>" clauses).
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
from typing import Optional

import requests

HEADERS = {"User-Agent": "sixdegrees-research/1.0 (john@sixdegrees.net)"}
BASE = "https://www.dignitymemorial.com"
SOURCE_DATA = "DIGNITYMEMORIAL"

# Single funeral-home-chain site, not a large news CMS with CDN capacity
# behind it -- be a polite, small crawler even though volume per run is low.
_REQUEST_DELAY_SECONDS = 1.0

DB_PATH_DEFAULT = "C:/Users/johnk/data/pipeline_cache.db"


# ---------------------------------------------------------------------------
# Name -> URL / candidate resolution
# ---------------------------------------------------------------------------

def _slugify_name_part(part: str) -> str:
    """Best-effort URL-part slug ("John" -> "john", "Mary-Jane" -> "mary-jane").
    Not yet validated against multi-word or hyphenated real names beyond the
    single sample checked during investigation -- a wrong slug just 404s
    (handled gracefully), it doesn't silently mismatch."""
    part = unicodedata.normalize("NFKD", part).encode("ascii", "ignore").decode("ascii")
    part = re.sub(r"[^a-zA-Z0-9\- ]", "", part).strip().lower()
    return re.sub(r"\s+", "-", part)


def _fetch_next_data(url: str) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            return None
    except requests.RequestException:
        return None
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (ValueError, TypeError):
        return None


def fetch_candidates(first: str, last: str) -> list[dict]:
    """Fetch the /obituaries/name/{first}/{last} disambiguation page and
    return its candidate list (each with firstName, lastName, displayName,
    birthDate, deathDate, link, obituaryText snippet). Empty list on no
    match or fetch failure -- callers should not distinguish the two."""
    url = f"{BASE}/obituaries/name/{_slugify_name_part(first)}/{_slugify_name_part(last)}"
    data = _fetch_next_data(url)
    if not data:
        return []
    try:
        return data["props"]["pageProps"]["obitsInit"]["results"]
    except (KeyError, TypeError):
        return []


def resolve_unambiguous_candidate(first: str, last: str) -> Optional[dict]:
    """Only returns a candidate when the name resolves to exactly one
    person -- a common name with several candidates is skipped rather than
    guessed at (same confidence-gate philosophy as
    webapp/pathfinder.py's _qa_find_person())."""
    candidates = fetch_candidates(first, last)
    if len(candidates) == 1:
        return candidates[0]
    return None


def fetch_full_obituary(link: str) -> Optional[dict]:
    """Fetch one individual obituary page, return {"text", "first_name",
    "last_name", "birth_date", "death_date"} or None."""
    data = _fetch_next_data(link)
    if not data:
        return None
    try:
        obit = data["props"]["pageProps"]["obituary"]
        subject = obit.get("Subject", {})
        text = obit.get("Obituary")
        if not text:
            return None
        return {
            "text": text,
            "first_name": subject.get("FirstName"),
            "last_name": subject.get("LastName"),
            "birth_date": subject.get("BirthDate"),
            "death_date": subject.get("DeathDate"),
        }
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Family relationship extraction
# ---------------------------------------------------------------------------

_REL_LABELS = [
    # in-laws, singular + the plural forms real obituaries actually use
    # ("sister-in-laws" is ungrammatical but genuinely appears verbatim)
    "son-in-law", "daughter-in-law", "sister-in-law", "brother-in-law",
    "father-in-law", "mother-in-law", "sister-in-laws", "brother-in-laws",
    "sisters-in-law", "brothers-in-law", "sons-in-law", "daughters-in-law",
    # step-family
    "stepson", "stepsons", "stepdaughter", "stepdaughters",
    "stepmother", "stepfather", "stepsister", "stepsisters",
    "stepbrother", "stepbrothers",
    # former spouse
    "former wife", "former husband", "former spouse", "ex-wife", "ex-husband",
    # core + plurals
    "grandchildren", "grandchild", "grandson", "grandsons",
    "granddaughter", "granddaughters",
    "wife", "husband", "daughters", "daughter", "sons", "son",
    "sisters", "sister", "brothers", "brother",
    "mother", "father", "parents", "siblings", "sibling",
]
_LABEL_ALTERNATION = "|".join(sorted(_REL_LABELS, key=len, reverse=True))
_LABEL_FINDITER = re.compile(
    rf"\b(?:devoted |beloved |cherished |loving |dear )?({_LABEL_ALTERNATION})\b"
    r"(?:\s+of\s+\d+\s+years)?\s*,?",
    re.IGNORECASE,
)
_SPOUSE_LABELS = {"wife", "husband", "former wife", "former husband", "former spouse", "ex-wife", "ex-husband"}

# A real obituary commonly has SEVERAL separate "survived by"/"preceded in
# death by" sentences scattered across paragraphs (Bemis sample: five
# separate "survived by ..." sentences) -- finditer over all of them, each
# clause bounded strictly at its own next period, not a single .search()
# spanning from the first anchor to some later stopping point. That greedy
# single-match version let one clause run past its actual sentence end (a
# stray "Peter is survived by his wife Susan" fragment leaking into the
# PRECEDING clause's captured text) -- confirmed via a real test run
# against the Bemis sample, same failure class as the org-name overrun
# below.
_PRECEDED_RE = re.compile(
    r"(?:preceded in death by|predeceased by)(?P<clause>.+?)(?:\.\s+[A-Z]|\.\s*\n|\.\s*$)",
    re.IGNORECASE | re.DOTALL,
)
_SURVIVED_RE = re.compile(
    r"survived by(?P<clause>.+?)(?:\.\s+[A-Z]|\.\s*\n|\.\s*$)",
    re.IGNORECASE | re.DOTALL,
)


def _strip_maiden_name(name: str) -> str:
    return re.sub(r"\s*\(n[ée]e[^)]*\)", "", name, flags=re.IGNORECASE).strip()


# "Dana (Broberg) of Sheboygan" -> "Dana (Broberg)": obituaries routinely
# trail a name with a "of <city>[, <ST>]" home-town clause (same pattern
# Kenya's newspaper_families.py had to handle for its own trailing
# location clauses) -- confirmed via a real test run against the Bemis
# sample, which has this on nearly every extended-family entry.
_TRAILING_PLACE_RE = re.compile(r"\s+of\s+(?:[A-Z][a-zA-Z.'\-]*\s*)+(?:,\s*[A-Z]{2})?$")


def _clean_extracted_name(name: str) -> str:
    name = _strip_maiden_name(name.strip())
    name = re.sub(r"^(?:and\s+|as well as\s+)", "", name, flags=re.IGNORECASE).strip()
    name = _TRAILING_PLACE_RE.sub("", name).strip()
    return name.rstrip(".").strip()


def _extract_clause_people(clause: str) -> list[tuple[str, str]]:
    """clause e.g. 'his devoted wife of 58 years, Marilyn Keyes; his
    daughter, Kathryn Guthrie; ...' -> [("wife", "Marilyn Keyes"), ...].

    Two-pass: find every label's position first, then slice the name-text
    between consecutive labels -- a single regex matching "label ... names
    until next boundary" swallowed unmarked labels that lacked a leading
    "his"/"her" (e.g. "..., daughter-in-law, Michelle Keyes and sister
    Patricia Prower" got eaten whole by the PRECEDING label's match before
    this fix), confirmed via a real test run against the Keyes sample.
    """
    labels = list(_LABEL_FINDITER.finditer(clause))
    results = []
    for i, m in enumerate(labels):
        label = m.group(1).lower()
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(clause)
        segment = re.split(r";", clause[start:end])[0]
        names_text = re.sub(r"\s+and\s+", ", ", segment)
        for raw_name in names_text.split(","):
            name = _clean_extracted_name(raw_name)
            # A bare first name with no capitalized second token ("Abigael"
            # from "grandchildren, Abigael and Liam Guthrie" -- shares Liam's
            # surname in the prose but that's not recoverable here) is kept,
            # not dropped: it just won't match anything at graph-build time,
            # which is the safe failure mode, not a wrong edge.
            if name and re.search(r"[A-Z][a-z]+", name) and len(name.split()) <= 5:
                results.append((label, name))
    return results


def extract_family_links(obituary_text: str) -> list[dict]:
    """Returns [{"relation_type": "SPOUSE"|"FAMILY", "relative_name": ...,
    "role": "wife"|"son"|..., "predeceased": bool}, ...]. Dedupes on
    (relative_name.lower(), predeceased) -- some obituaries mention the same
    relative in more than one "survived by" sentence."""
    links = []
    seen = set()

    def _add(clause, predeceased):
        for role, name in _extract_clause_people(clause):
            key = (name.lower(), predeceased)
            if key in seen:
                continue
            seen.add(key)
            links.append({
                "relation_type": "SPOUSE" if role in _SPOUSE_LABELS else "FAMILY",
                "relative_name": name, "role": role, "predeceased": predeceased,
            })

    for m in _PRECEDED_RE.finditer(obituary_text):
        _add(m.group("clause"), True)
    for m in _SURVIVED_RE.finditer(obituary_text):
        _add(m.group("clause"), False)
    return links


# ---------------------------------------------------------------------------
# Organization affiliation extraction
# ---------------------------------------------------------------------------

# Solid, validated against real data: directly overlaps with SEC-sourced
# board membership already in the graph, the single highest-value pattern.
_BOARD_RE = re.compile(
    # (?i:...) scopes case-insensitivity to just the anchor phrase -- the
    # capture group's [A-Z] must stay genuinely case-SENSITIVE, or IGNORECASE
    # (needed so "Board of Directors"/"board of directors" both match) also
    # makes [A-Z&] match lowercase, silently defeating the whole point of
    # requiring capitalized words to bound the org-name capture. Confirmed
    # via a real test run: this let "St. Nicholas Hospital and Lakeland
    # College" merge into one false two-org string through the lowercase
    # "and".
    r"(?i:board of directors)\s+(?i:for|of|at)\s+(?:the\s+)?([A-Z][\w&,\u2019'\-]*\.?(?:\s+[A-Z&][\w&,\u2019'\-]*\.?){0,5})"
)

# Best-effort: only captures the first org when a sentence lists several
# ("Mine Manager at Mattabi, Geco and Hemlo Mines" -> "Mattabi" only).
_EXEC_TITLES = [
    "Chief Executive Officer", "Chief Operating Officer", "Chief Financial Officer",
    "Chief Technology Officer", "Chief Investment Officer", "President", "Chairman",
    "CEO", "CFO", "COO", "CTO", "Executive Director", "Managing Director",
    "General Counsel", "Mine Manager", "Plant Manager", "General Manager",
]
_EXEC_TITLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_EXEC_TITLES, key=len, reverse=True)) + r")"
    r"\s+(?:of|at|for|with)\s+(?:the\s+)?([A-Z][\w&,\u2019'\-]*\.?(?:\s+[A-Z&][\w&,\u2019'\-]*\.?){0,5})",
)

_TITLE_TO_RELATION_TYPE = {
    "chief executive officer": "CHIEF_EXECUTIVE_OFFICER", "ceo": "CHIEF_EXECUTIVE_OFFICER",
    "chief operating officer": "CHIEF_OPERATING_OFFICER", "coo": "CHIEF_OPERATING_OFFICER",
    "chief financial officer": "CHIEF_FINANCIAL_OFFICER", "cfo": "CHIEF_FINANCIAL_OFFICER",
    "chief technology officer": "CHIEF_TECHNOLOGY_OFFICER", "cto": "CHIEF_TECHNOLOGY_OFFICER",
    "chief investment officer": "CHIEF_INVESTMENT_OFFICER",
    "president": "PRESIDENT", "chairman": "CHAIRMAN",
    "executive director": "EXECUTIVE_DIRECTOR", "managing director": "MANAGING_DIRECTOR",
    "general counsel": "GENERAL_COUNSEL",
    # Not in webapp/relation_categories.py's categorize() list -- these land
    # in the OTHER bucket until/unless a future pass adds them. Documented,
    # not a bug: still written to the graph, just not specially categorized.
    "mine manager": "MINE_MANAGER", "plant manager": "PLANT_MANAGER",
    "general manager": "GENERAL_MANAGER",
}


# Final safety net: even with the org regex bounded to <=6 capitalized
# words, a run of proper nouns in narrative prose (people's names, place
# names right after the real org) can still produce a plausible-LOOKING
# but wrong capture. A capitalized common word from ordinary sentence flow
# ("He", "She", "Said", "During", ...) appearing as one of the words is a
# strong signal of exactly that -- confirmed via a real false-positive
# ("...Lakeland College. He was active...") in a test run against the
# Bemis sample before this filter was added.
_NARRATIVE_FILLER_WORDS = {
    "he", "she", "his", "her", "they", "said", "was", "is", "were", "are",
    "during", "after", "before", "also", "further", "recently", "the",
}


def _looks_like_real_org(name: str) -> bool:
    words = name.split()
    if not words or len(words) > 6:
        return False
    if any(w.strip(".,").lower() in _NARRATIVE_FILLER_WORDS for w in words):
        return False
    return True


def _clean_org_name(name: str) -> str:
    return name.strip().rstrip(".,;").strip()


def extract_org_links(obituary_text: str) -> list[dict]:
    """Returns [{"relation_type": ..., "org_name": ...}, ...]. Board
    membership is deduped against title matches for the same org (a person
    described as both "on the board of X" and, elsewhere, "Chairman of X"
    only yields the more specific BOARD_MEMBER row)."""
    links = []
    seen_orgs = set()
    for m in _BOARD_RE.finditer(obituary_text):
        org = _clean_org_name(m.group(1))
        if org and _looks_like_real_org(org) and org.lower() not in seen_orgs:
            links.append({"relation_type": "BOARD_MEMBER", "org_name": org})
            seen_orgs.add(org.lower())
    for m in _EXEC_TITLE_RE.finditer(obituary_text):
        title, org = m.group(1).lower(), _clean_org_name(m.group(2))
        if org and _looks_like_real_org(org) and org.lower() not in seen_orgs:
            links.append({"relation_type": _TITLE_TO_RELATION_TYPE.get(title, "OTHER"), "org_name": org})
            seen_orgs.add(org.lower())
    return links


# ---------------------------------------------------------------------------
# DB write path
# ---------------------------------------------------------------------------

def _ensure_tracking_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS obituary_lookup_attempts (
            first_name TEXT, last_name TEXT, outcome TEXT,
            obituary_url TEXT, attempted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (first_name, last_name)
        )
    """)
    conn.commit()


def _already_attempted(conn, first: str, last: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM obituary_lookup_attempts WHERE first_name = ? AND last_name = ?",
        (first.lower(), last.lower()),
    ).fetchone()
    return row is not None


def _record_attempt(conn, first: str, last: str, outcome: str, url: Optional[str]):
    conn.execute(
        "INSERT OR REPLACE INTO obituary_lookup_attempts (first_name, last_name, outcome, obituary_url) "
        "VALUES (?, ?, ?, ?)",
        (first.lower(), last.lower(), outcome, url),
    )
    conn.commit()


def write_links(conn, deceased_name: str, url: str, family_links: list[dict], org_links: list[dict]) -> int:
    inserted = 0
    for link in family_links:
        evidence = json.dumps({"role": link["role"], "predeceased": link["predeceased"], "url": url})
        conn.execute(
            "INSERT INTO relationships (source_name, source_type, target_name, target_type, "
            "relation_type, source_data, evidence) VALUES (?, 'PERSON', ?, 'PERSON', ?, ?, ?)",
            (deceased_name, link["relative_name"], link["relation_type"], SOURCE_DATA, evidence),
        )
        inserted += 1
    for link in org_links:
        evidence = json.dumps({"url": url})
        conn.execute(
            "INSERT INTO relationships (source_name, source_type, target_name, target_type, "
            "relation_type, source_data, evidence) VALUES (?, 'PERSON', ?, 'ORG', ?, ?, ?)",
            (deceased_name, link["org_name"], link["relation_type"], SOURCE_DATA, evidence),
        )
        inserted += 1
    conn.commit()
    return inserted


def lookup_and_write(conn, first: str, last: str, dry_run: bool = False) -> dict:
    """End-to-end: resolve -> fetch -> extract -> (optionally) write.
    Always records the attempt (even a skip/no-match) so a re-run over the
    same name list doesn't re-fetch unless the caller clears the tracking
    table -- mirrors ripkenya_obituaries.py's processed-URL tracking."""
    if not dry_run and _already_attempted(conn, first, last):
        return {"outcome": "already_attempted"}

    candidate = resolve_unambiguous_candidate(first, last)
    time.sleep(_REQUEST_DELAY_SECONDS)
    if not candidate:
        if not dry_run:
            _record_attempt(conn, first, last, "no_unambiguous_match", None)
        return {"outcome": "no_unambiguous_match"}

    obit = fetch_full_obituary(candidate["link"])
    time.sleep(_REQUEST_DELAY_SECONDS)
    if not obit:
        if not dry_run:
            _record_attempt(conn, first, last, "fetch_failed", candidate["link"])
        return {"outcome": "fetch_failed", "link": candidate["link"]}

    deceased_name = f"{obit['first_name']} {obit['last_name']}".strip()
    family_links = extract_family_links(obit["text"])
    org_links = extract_org_links(obit["text"])

    result = {
        "outcome": "extracted", "deceased_name": deceased_name, "link": candidate["link"],
        "family_links": family_links, "org_links": org_links,
    }
    if not dry_run:
        n = write_links(conn, deceased_name, candidate["link"], family_links, org_links)
        _record_attempt(conn, first, last, "extracted", candidate["link"])
        result["rows_written"] = n
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="+", help='Full names to look up, e.g. "John Keyes" "Jane Smith"')
    ap.add_argument("--dry-run", action="store_true", help="Extract and print, don't touch the DB")
    ap.add_argument("--db", default=DB_PATH_DEFAULT)
    args = ap.parse_args()

    conn = None
    if not args.dry_run:
        conn = sqlite3.connect(args.db)
        _ensure_tracking_table(conn)

    for full_name in args.names:
        parts = full_name.strip().split()
        if len(parts) < 2:
            print(f"SKIP (need first+last): {full_name!r}")
            continue
        first, last = parts[0], parts[-1]
        result = lookup_and_write(conn, first, last, dry_run=args.dry_run)
        print(f"{full_name}: {result['outcome']}")
        if result["outcome"] == "extracted":
            print(f"  matched: {result['deceased_name']} ({result['link']})")
            for role, name in [(l["role"], l["relative_name"]) for l in result["family_links"]]:
                print(f"    FAMILY  [{role}] {name}")
            for link in result["org_links"]:
                print(f"    ORG     [{link['relation_type']}] {link['org_name']}")
            if "rows_written" in result:
                print(f"  wrote {result['rows_written']} rows")

    if conn:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
