"""Years a person was at an organization, read from a link's evidence
(build-time only; used by build_scored_edges.py). User decisions, 2026-11-22:

  * Two people at the same organization are linked at full strength only when
    their years there overlap. People whose years don't overlap (or whose
    years aren't known) still connect through the organization, but weakly --
    the same rule as alumni class years.
  * A source that gives a real start and end (SEC insider filings, LittleSis,
    congressional terms) is used as is. A source that gives a single filing
    year Y (Form D, nonprofit 990s, union LM-2 reports, lobbying registrations,
    patents) counts as "there around Y-1 to Y+1", which on 4-year era nodes
    guarantees two single-year people within 2 years of each other share a
    node (and sometimes matches people up to 5 years apart).

years_for() returns (first_year, last_year) or None. era_buckets() turns that
range into the 4-year era nodes the person joins: two people share an era
node when their ranges overlap or come within a few years. (2-year nodes with
+/-2 slack gave the same matching at about twice the edges -- shrunk
2026-11-22, test graph went 7.6M -> 12.0M edges.)
"""
import json
import re

POINT_SLACK = 1          # a single filing year Y means Y-1 .. Y+1
OPEN_SPAN = 4            # a start with no end (or an end with no start): 4 more years
MAX_SPAN = 30            # longer ranges are clipped (bad data, or a lifetime seat)
BUCKET = 4               # era nodes are 4-year windows: 2012-2015, 2016-2019, ...
MIN_YEAR = 1900

# Sources whose evidence carries dates, and the person -> organization
# relations among them that are a seat, job or membership (not a donation).
DATED_SOURCES = ("SEC_FORM_345", "LITTLESIS", "CONGRESS_GITHUB", "SEC_FORM_D",
                 "IRS_990_TEOS", "OLMS_LM2", "LDA_LOBBYISTS", "PATENT_COINVENTOR")

_YEAR = re.compile(r"(1[89]\d\d|20\d\d)")


def _year(v):
    if v is None:
        return None
    m = _YEAR.match(str(v).strip())
    return int(m.group(1)) if m else None


def _point(y):
    return (y - POINT_SLACK, y + POINT_SLACK)


def years_for(source, evidence, now_year):
    """(first, last) for one person -> organization link, or None when its
    evidence has no usable date."""
    if not evidence:
        return None
    try:
        ev = json.loads(evidence)
    except (ValueError, TypeError):
        return None
    if not isinstance(ev, dict):
        return None
    rng = None
    if source == "SEC_FORM_345":
        a, b = _year(ev.get("first_filing")), _year(ev.get("last_filing"))
        if ev.get("current"):
            b = now_year
        if a and b:
            rng = (a, b)
        elif a or b:
            rng = _point(a or b)
    elif source == "CONGRESS_GITHUB":
        a, b = _year(ev.get("term_start")), _year(ev.get("term_end"))
        if a:
            rng = (a, min(b or a + OPEN_SPAN, now_year))
    elif source == "LITTLESIS":
        a, b = _year(ev.get("start_date")), _year(ev.get("end_date"))
        if a and b:
            rng = (a, b)
        elif a:
            rng = (a, min(a + OPEN_SPAN, now_year))
        elif b:
            rng = (b - OPEN_SPAN, b)
    elif source == "SEC_FORM_D":
        y = _year(ev.get("filing_date")) or _year((ev.get("quarter") or "")[:4])
        rng = _point(y) if y else None
    elif source == "IRS_990_TEOS":
        # "2019/download990xml_2019_4.zip": the year the IRS released the
        # filing, which mostly covers the tax year before it
        y = _year((ev.get("filing") or "")[:4])
        rng = _point(y - 1) if y else None
    elif source == "OLMS_LM2":
        y = _year(ev.get("year"))
        rng = _point(y) if y else None
    elif source == "LDA_LOBBYISTS":
        y = _year(ev.get("filing_year"))
        rng = _point(y) if y else None
    elif source == "PATENT_COINVENTOR":
        y = _year(ev.get("filing_date"))
        rng = _point(y) if y else None
    if not rng:
        return None
    a, b = rng
    if a > b:
        a, b = b, a
    a, b = max(a, MIN_YEAR), min(b, now_year)
    if a > b:
        return None
    if b - a > MAX_SPAN:
        a = b - MAX_SPAN
    return (a, b)


def era_buckets(first, last):
    """Start years of the 2-year era windows that [first, last] touches."""
    start = first - (first % BUCKET)
    return list(range(start, last + 1, BUCKET))


def era_label(org_display, bucket_start):
    return f"{org_display} (in {bucket_start}–{bucket_start + BUCKET - 1})"


# Matches an era node's label (pathfinder hides these from search, as it does
# class-year nodes).
ERA_LABEL_RE = re.compile(r".* \(in \d{4}–\d{4}\)$")
