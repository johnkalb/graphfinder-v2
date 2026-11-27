"""Getting users to help build the graph -- Phase 1 (plan: ~/.claude/plans/
peppy-imagining-lynx.md, approved 2026-11-27).

Most new users know nobody in the notable network, so the first payoff can't
be "your strong path to a board member". It is:
  * "Where have you been?" -- organizations and schools the user belonged to,
    with years. With years, the user joins that organization's period nodes
    ("Acme (in 2016–2019)", affiliation_years.py) at full strength, like
    anyone else who was there then; otherwise only weakly, through the
    organization itself -- the same rule the nightly build applies.
  * "You are N steps from ..." -- distance to a few famous people through
    any of the user's links or affiliations, weak ones included (the fun is
    the distance; paths good enough for an introduction are labelled apart).
  * "Who from your world is in here" -- graph people at the user's
    organizations in the user's years.

Affiliations are private: only the owner sees them, they're used only in the
owner's own searches, and the nightly build never reads them.

Pure database + graph-agnostic logic; graph lookups are passed in by
pathfinder.py (so this is testable without loading the graph).
"""
import datetime

try:
    import db
    from affiliation_years import BUCKET, era_buckets, era_label
except ImportError:  # imported as webapp.contrib
    from webapp import db
    from webapp.affiliation_years import BUCKET, era_buckets, era_label

MAX_AFFILIATIONS = 30
AFF_STRONG = 0.85      # the user's own seat/job/class at a period they were there
AFF_WEAK = 0.15        # ORG_AFFILIATION: the organization, years not shared
MIN_YEAR = 1930

# "You are N steps from ...": well-known names tried in order; those missing
# from the graph are skipped. Kept to people across fields, not one party.
FAMOUS = ["Barack Obama", "Donald Trump", "Warren Buffett", "Oprah Winfrey", "Bill Gates",
          "Taylor Swift", "Tom Hanks", "Serena Williams", "Elon Musk", "Dolly Parton"]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _clean_year(y, now_year):
    if y in (None, ""):
        return None
    try:
        y = int(y)
    except (TypeError, ValueError):
        raise ValueError("years must be numbers like 2016")
    if not (MIN_YEAR <= y <= now_year):
        raise ValueError(f"years must be between {MIN_YEAR} and {now_year}")
    return y


def add_affiliation(db_path, owner, org, start_year=None, end_year=None, *, now_year=None):
    """(row, None) or (None, error). `org` is the graph node the user picked."""
    now_year = now_year or datetime.date.today().year
    org = (org or "").strip()
    if not org:
        return None, "pick an organization or school"
    try:
        a, b = _clean_year(start_year, now_year), _clean_year(end_year, now_year)
    except ValueError as e:
        return None, str(e)
    if a and b and a > b:
        a, b = b, a
    conn = db.connect(db_path)
    n = conn.execute("SELECT count(*) AS n FROM my_affiliations WHERE owner_email = ? AND deleted_at IS NULL",
                     (owner,)).fetchone()["n"]
    if n >= MAX_AFFILIATIONS:
        conn.close()
        return None, f"You can add up to {MAX_AFFILIATIONS} places."
    conn.execute("INSERT INTO my_affiliations (owner_email, org, start_year, end_year, created_at) "
                 "VALUES (?, ?, ?, ?, ?)", (owner, org, a, b, _now()))
    conn.commit()
    row = conn.execute("SELECT id, org, start_year, end_year, created_at FROM my_affiliations "
                       "WHERE owner_email = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1", (owner,)).fetchone()
    conn.close()
    return dict(row), None


def list_affiliations(db_path, owner):
    conn = db.connect(db_path)
    rows = conn.execute("SELECT id, org, start_year, end_year, created_at FROM my_affiliations "
                        "WHERE owner_email = ? AND deleted_at IS NULL ORDER BY id", (owner,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_affiliation(db_path, owner, aff_id):
    conn = db.connect(db_path)
    cur = conn.execute("UPDATE my_affiliations SET deleted_at = ? WHERE id = ? AND owner_email = ? "
                       "AND deleted_at IS NULL", (_now(), aff_id, owner))
    conn.commit()
    ok = (cur.rowcount or 0) > 0
    conn.close()
    return ok


def delete_account_affiliations(db_path, owner):
    conn = db.connect(db_path)
    conn.execute("UPDATE my_affiliations SET deleted_at = ? WHERE owner_email = ? AND deleted_at IS NULL",
                 (_now(), owner))
    conn.commit()
    conn.close()


def years_text(aff):
    a, b = aff.get("start_year"), aff.get("end_year")
    if a and b:
        return f"{a}–{b}" if a != b else str(a)
    if a:
        return f"from {a}"
    if b:
        return f"until {b}"
    return ""


def period_nodes(org, has_node, now_year=None):
    """{bucket_start: node name} for the organization's period nodes in the graph."""
    now_year = now_year or datetime.date.today().year
    out = {}
    for start in range(MIN_YEAR - MIN_YEAR % BUCKET, now_year + 1, BUCKET):
        name = era_label(org, start)
        if has_node(name):
            out[start] = name
    return out


def user_span(aff, now_year=None):
    """(first, last) years the user gave, or None. One year given = that year
    (plus one either side, like a single filing year in the nightly build)."""
    now_year = now_year or datetime.date.today().year
    a, b = aff.get("start_year"), aff.get("end_year")
    if a and b:
        return a, b
    if a:                       # "from 2016": still there
        return a, now_year
    if b:
        return max(MIN_YEAR, b - 1), b
    return None


def entry_points(affs, has_node, now_year=None):
    """Where each affiliation puts the user in the graph:
    [{"node", "prob", "aff", "kind": "period"|"org"}].

    An organization with period nodes (10+ dated people in the nightly build):
    full strength into the periods the user's years touch, weak into the
    organization itself. An organization without them: full strength into the
    organization, as for everyone else listed there."""
    out = []
    for aff in affs:
        org = aff["org"]
        if not has_node(org):
            continue
        periods = period_nodes(org, has_node, now_year)
        span = user_span(aff, now_year)
        if not periods:
            out.append({"node": org, "prob": AFF_STRONG, "aff": aff, "kind": "org"})
            continue
        hit = False
        if span:
            for start in era_buckets(*span):
                if start in periods:
                    out.append({"node": periods[start], "prob": AFF_STRONG, "aff": aff, "kind": "period"})
                    hit = True
        out.append({"node": org, "prob": AFF_WEAK, "aff": aff, "kind": "org"})
        aff["_matched_periods"] = hit
    return out
