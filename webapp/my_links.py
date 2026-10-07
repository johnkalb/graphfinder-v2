"""Each user's own links -- private or public (private-service spec, 2026-10-06).

Spec decisions this implements (plan: ~/.claude/plans/resilient-doodling-graham.md):
  1  every link is private or public; public ones join the shared graph
  2  a link may be public only if the other person is a verified public figure
  3  public links are weak and labelled self-reported, capped per user, reportable
  10 deleting is honoured at once, everywhere
  11 existing approved self-reported links became private links
  12 the user agreement must be accepted first

A contact the user couldn't find in the graph ("None of these") or whose
match was a namesake ("Wrong person") is kept as an UNMATCHED link: private,
never in searches, kept so it can be matched once the person is in the graph.

Pure database functions over the app DB (db.connect, '?' placeholders, so the
same code runs on SQLite in tests and Postgres in production); the HTTP layer
lives in pathfinder.py.
"""
from datetime import datetime, timezone

try:
    import db
except ImportError:  # imported as webapp.my_links
    from webapp import db

MAX_PUBLIC_LINKS = 50
AGREEMENT_VERSION = "beta-1"
SELF_REPORTED = ("SELF_ATTESTED_CONTACT", "LINKEDIN_CONNECTION")
UNMATCHED = "UNMATCHED"   # contact_type of a contact with no graph node (yet)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row(r):
    return dict(r) if r is not None else None


# --- user agreement ------------------------------------------------------------

def agreement_accepted(db_path, email, version=AGREEMENT_VERSION):
    conn = db.connect(db_path)
    r = conn.execute("SELECT 1 FROM user_agreements WHERE email = ? AND version = ?",
                     (email, version)).fetchone()
    conn.close()
    return r is not None


def accept_agreement(db_path, email, version=AGREEMENT_VERSION):
    if agreement_accepted(db_path, email, version):
        return
    conn = db.connect(db_path)
    conn.execute("INSERT INTO user_agreements (email, version, accepted_at) VALUES (?, ?, ?)",
                 (email, version, _now()))
    conn.commit()
    conn.close()


# --- links ----------------------------------------------------------------------

def list_links(db_path, owner):
    conn = db.connect(db_path)
    rows = conn.execute("SELECT id, contact, contact_type, relation, source, visibility, created_at, report_count "
                        "FROM my_links WHERE owner_email = ? AND deleted_at IS NULL ORDER BY contact",
                        (owner,)).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def public_count(db_path, owner):
    conn = db.connect(db_path)
    r = conn.execute("SELECT count(*) AS n FROM my_links WHERE owner_email = ? AND visibility = 'public' "
                     "AND deleted_at IS NULL", (owner,)).fetchone()
    conn.close()
    return int(r["n"])


def _check_public(db_path, owner, contact, is_verified, exclude_id=None, contact_type="PERSON"):
    """None if the link may be public, else the reason it may not."""
    if contact_type == UNMATCHED:
        return "This contact isn't matched to anyone in the database, so it can only be private."
    if not is_verified(contact):
        return ("Only links to verified public figures can be public; links to private people "
                "stay private to protect them.")
    n = public_count(db_path, owner)
    if exclude_id is None and n >= MAX_PUBLIC_LINKS:
        return f"You already have the maximum of {MAX_PUBLIC_LINKS} public links."
    return None


def add_link(db_path, owner, contact, *, contact_type="PERSON", relation="SELF_ATTESTED_CONTACT",
             source=None, visibility="private", is_verified=lambda name: False):
    """-> (link dict, None) or (None, error). Re-adding an existing active link
    returns it unchanged (and makes it public if asked and allowed)."""
    if visibility not in ("private", "public"):
        return None, "visibility must be 'private' or 'public'"
    conn = db.connect(db_path)
    existing = conn.execute("SELECT id, visibility FROM my_links WHERE owner_email = ? AND contact = ? "
                            "AND contact_type = ? AND deleted_at IS NULL", (owner, contact, contact_type)).fetchone()
    conn.close()
    if existing:
        if visibility == "public" and existing["visibility"] != "public":
            return set_visibility(db_path, owner, existing["id"], "public", is_verified)
        return get_link(db_path, owner, existing["id"]), None
    if visibility == "public":
        err = _check_public(db_path, owner, contact, is_verified, contact_type=contact_type)
        if err:
            return None, err
    conn = db.connect(db_path)
    if contact_type != UNMATCHED:
        # matching a contact that was waiting as UNMATCHED replaces the placeholder
        i = contact.find(" (")
        base = contact[:i] if contact.endswith(")") and i > 0 else contact
        conn.execute("UPDATE my_links SET deleted_at = ? WHERE owner_email = ? AND contact_type = ? "
                     "AND lower(contact) = lower(?) AND deleted_at IS NULL", (_now(), owner, UNMATCHED, base))
    conn.execute("INSERT INTO my_links (owner_email, contact, contact_type, relation, source, visibility, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (owner, contact, contact_type, relation, source, visibility, _now()))
    conn.commit()
    r = conn.execute("SELECT id FROM my_links WHERE owner_email = ? AND contact = ? AND contact_type = ? "
                     "AND deleted_at IS NULL", (owner, contact, contact_type)).fetchone()
    conn.close()
    return get_link(db_path, owner, r["id"]), None


def add_unmatched(db_path, owner, contact, source=None):
    """'None of these': keep the contact, privately, without a graph match."""
    contact = " ".join((contact or "").split())
    if not contact:
        return None, "contact name required"
    relation = "LINKEDIN_CONNECTION" if source == "linkedin" else "SELF_ATTESTED_CONTACT"
    return add_link(db_path, owner, contact, contact_type=UNMATCHED, relation=relation, source=source)


def mark_wrong_person(db_path, owner, link_id):
    """'Wrong person': the match was a namesake. The link leaves searches at
    once (a public one is withdrawn like a delete) and the contact is kept as
    UNMATCHED under the base name, e.g. 'Mark Greene (Ibm)' -> 'Mark Greene'."""
    link = get_link(db_path, owner, link_id)
    if not link:
        return None, "link not found"
    if link["contact_type"] == UNMATCHED:
        return link, None
    name = link["contact"].strip()
    i = name.find(" (")
    if name.endswith(")") and i > 0:
        name = name[:i]
    delete_link(db_path, owner, link_id)
    return add_unmatched(db_path, owner, name, link["source"])


def get_link(db_path, owner, link_id):
    conn = db.connect(db_path)
    r = conn.execute("SELECT id, contact, contact_type, relation, source, visibility, created_at, report_count "
                     "FROM my_links WHERE id = ? AND owner_email = ? AND deleted_at IS NULL",
                     (link_id, owner)).fetchone()
    conn.close()
    return _row(r)


def set_visibility(db_path, owner, link_id, visibility, is_verified=lambda name: False):
    link = get_link(db_path, owner, link_id)
    if not link:
        return None, "link not found"
    if visibility not in ("private", "public"):
        return None, "visibility must be 'private' or 'public'"
    if visibility == "public" and link["visibility"] != "public":
        err = _check_public(db_path, owner, link["contact"], is_verified, contact_type=link["contact_type"])
        if err:
            return None, err
    if visibility == "private" and link["visibility"] == "public":
        # going private = withdrawing it from the shared graph, honoured at once
        # like a delete: record the public version as removed, keep a private copy
        conn = db.connect(db_path)
        now = _now()
        conn.execute("UPDATE my_links SET deleted_at = ? WHERE id = ?", (now, link_id))
        conn.execute("INSERT INTO my_links (owner_email, contact, contact_type, relation, source, visibility, created_at) "
                     "VALUES (?, ?, ?, ?, ?, 'private', ?)",
                     (owner, link["contact"], link["contact_type"], link["relation"], link["source"], now))
        conn.commit()
        r = conn.execute("SELECT id FROM my_links WHERE owner_email = ? AND contact = ? AND contact_type = ? "
                         "AND deleted_at IS NULL", (owner, link["contact"], link["contact_type"])).fetchone()
        conn.close()
        return get_link(db_path, owner, r["id"]), None
    conn = db.connect(db_path)
    conn.execute("UPDATE my_links SET visibility = ? WHERE id = ?", (visibility, link_id))
    conn.commit()
    conn.close()
    return get_link(db_path, owner, link_id), None


def delete_link(db_path, owner, link_id):
    link = get_link(db_path, owner, link_id)
    if not link:
        return False
    conn = db.connect(db_path)
    conn.execute("UPDATE my_links SET deleted_at = ? WHERE id = ? AND owner_email = ?", (_now(), link_id, owner))
    conn.commit()
    conn.close()
    return True


def delete_account_links(db_path, owner):
    """Account deletion: every link goes (public ones become 'removed' for the sync)."""
    conn = db.connect(db_path)
    conn.execute("UPDATE my_links SET deleted_at = ? WHERE owner_email = ? AND deleted_at IS NULL", (_now(), owner))
    conn.execute("DELETE FROM user_agreements WHERE email = ?", (owner,))
    conn.commit()
    conn.close()


def report_link(db_path, owner_display_resolver, subject, contact):
    """Count a report against a PUBLIC link shown in someone's path. owner
    display names aren't stored, so match by contact and resolve owners."""
    conn = db.connect(db_path)
    rows = conn.execute("SELECT id, owner_email FROM my_links WHERE contact = ? AND visibility = 'public' "
                        "AND deleted_at IS NULL", (contact,)).fetchall()
    hit = None
    for r in rows:
        if (owner_display_resolver(r["owner_email"]) or "").lower() == (subject or "").lower():
            hit = r["id"]
            conn.execute("UPDATE my_links SET report_count = report_count + 1 WHERE id = ?", (hit,))
            break
    conn.commit()
    conn.close()
    return hit


# --- nightly sync and live filtering ----------------------------------------------

def public_links(db_path):
    conn = db.connect(db_path)
    rows = conn.execute("SELECT id, owner_email, contact, contact_type, relation FROM my_links "
                        "WHERE visibility = 'public' AND deleted_at IS NULL ORDER BY id").fetchall()
    conn.close()
    return [_row(r) for r in rows]


def removed_public_links(db_path):
    """Public links that were deleted or made private: the sync removes them
    from the harvest database, live searches filter them until it does."""
    conn = db.connect(db_path)
    rows = conn.execute("SELECT id, owner_email, contact, relation FROM my_links "
                        "WHERE visibility = 'public' AND deleted_at IS NOT NULL ORDER BY id").fetchall()
    conn.close()
    return [_row(r) for r in rows]


def migrate_legacy_user_links(db_path):
    """Decision 11: approved self-reported links from the old review flow
    (user_links) become their owners' private links. Idempotent."""
    conn = db.connect(db_path)
    rows = conn.execute("SELECT u.id, u.submitter_email, u.object, u.object_type, u.predicate, u.approved_at "
                        "FROM user_links u WHERE u.predicate IN (?, ?) AND u.submitter_email IS NOT NULL "
                        "AND NOT EXISTS (SELECT 1 FROM my_links m WHERE m.legacy_user_link_id = u.id)",
                        SELF_REPORTED).fetchall()
    for r in rows:
        dup = conn.execute("SELECT 1 FROM my_links WHERE owner_email = ? AND contact = ? AND deleted_at IS NULL",
                           (r["submitter_email"], r["object"])).fetchone()
        if dup:
            continue
        conn.execute("INSERT INTO my_links (owner_email, contact, contact_type, relation, source, visibility, "
                     "legacy_user_link_id, created_at) VALUES (?, ?, ?, ?, ?, 'private', ?, ?)",
                     (r["submitter_email"], r["object"], r["object_type"] or "PERSON", r["predicate"],
                      "linkedin" if r["predicate"] == "LINKEDIN_CONNECTION" else "contacts", r["id"],
                      r["approved_at"] or _now()))
    conn.commit()
    conn.close()
    return len(rows)


def export_csv(db_path, owner):
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["contact", "type", "relation", "source", "visibility", "added"])
    for l in list_links(db_path, owner):
        w.writerow([l["contact"], l["contact_type"], l["relation"], l["source"] or "", l["visibility"], l["created_at"]])
    return buf.getvalue()
