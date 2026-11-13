"""Team workspaces: a private overlay on the public graph (hooconnex Teams,
plan ~/.claude/plans/peppy-imagining-lynx.md, Phase 1, 2026-11-13).

An organization's workspace holds what its members upload -- today contact
lists, later relationship tables and CRM syncs. Nothing here ever enters the
shared graph, the nightly build, crawlies, the demo or admin screens; it is
merged only into that workspace's own searches.

Isolation rule: every function takes the workspace id AND the acting member's
email, and checks membership itself (`_member`). The HTTP layer never passes
a workspace id it hasn't had checked here, and non-members get "not found"
so a workspace's existence isn't revealed.

Personal "My links" (my_links.py) stays as each person's personal space;
these tables are for teams.

Pure database functions over the app DB (db.connect, '?' placeholders), like
my_links.py, so the same code runs on SQLite in tests and Postgres in prod.
"""
import csv
import io
import json
import re
from datetime import datetime, timezone

try:
    import db
except ImportError:  # imported as webapp.workspaces
    from webapp import db

ROLES = ("admin", "member")
DEFAULT_SEAT_LIMIT = 10
MAX_CONTACT_ROWS = 50_000
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MATCHED, AMBIGUOUS, UNMATCHED = "matched", "ambiguous", "unmatched"


class WorkspaceError(Exception):
    """A request the caller may not make; .status is the HTTP status to return."""
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _rows(rs):
    return [dict(r) for r in rs]


def _email(e):
    e = (e or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
        raise WorkspaceError("a valid email address is required")
    return e


# --- membership ----------------------------------------------------------------

def _member(conn, ws_id, email, role=None):
    """The member row, or WorkspaceError(404) -- also for workspaces that don't
    exist or were deleted, so outsiders can't probe for them."""
    r = conn.execute("SELECT m.role, w.name, w.seat_limit FROM workspace_members m "
                     "JOIN workspaces w ON w.id = m.workspace_id "
                     "WHERE m.workspace_id = ? AND m.email = ? AND m.removed_at IS NULL AND w.deleted_at IS NULL",
                     (ws_id, (email or "").lower())).fetchone()
    if r is None:
        raise WorkspaceError("Not Found", 404)
    if role == "admin" and r["role"] != "admin":
        raise WorkspaceError("only a workspace admin can do that", 403)
    return dict(r)


def audit(conn, ws_id, email, action, detail=None):
    conn.execute("INSERT INTO ws_audit (workspace_id, email, action, detail, at) VALUES (?, ?, ?, ?, ?)",
                 (ws_id, email, action, json.dumps(detail) if detail is not None else None, _now()))


def create_workspace(db_path, name, admin_email, created_by, seat_limit=DEFAULT_SEAT_LIMIT):
    """Operator action (site admins only, enforced by the HTTP layer): a new
    workspace with its first admin."""
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise WorkspaceError("a workspace name (up to 120 characters) is required")
    admin_email = _email(admin_email)
    conn = db.connect(db_path)
    try:
        conn.execute("INSERT INTO workspaces (name, seat_limit, created_by, created_at) VALUES (?, ?, ?, ?)",
                     (name, int(seat_limit), created_by, _now()))
        ws_id = conn.execute("SELECT max(id) AS id FROM workspaces WHERE name = ? AND created_by = ?",
                             (name, created_by)).fetchone()["id"]
        conn.execute("INSERT INTO workspace_members (workspace_id, email, role, added_by, added_at) "
                     "VALUES (?, ?, 'admin', ?, ?)", (ws_id, admin_email, created_by, _now()))
        audit(conn, ws_id, created_by, "workspace_created", {"name": name, "admin": admin_email})
        conn.commit()
    finally:
        conn.close()
    return ws_id


def my_workspaces(db_path, email):
    conn = db.connect(db_path)
    try:
        return _rows(conn.execute(
            "SELECT w.id, w.name, m.role, w.seat_limit FROM workspace_members m "
            "JOIN workspaces w ON w.id = m.workspace_id "
            "WHERE m.email = ? AND m.removed_at IS NULL AND w.deleted_at IS NULL ORDER BY w.name",
            ((email or "").lower(),)).fetchall())
    finally:
        conn.close()


def get_workspace(db_path, ws_id, email):
    conn = db.connect(db_path)
    try:
        m = _member(conn, ws_id, email)
        n = conn.execute("SELECT count(*) AS n FROM workspace_members WHERE workspace_id = ? AND removed_at IS NULL",
                         (ws_id,)).fetchone()["n"]
        e = conn.execute("SELECT status, count(*) AS n FROM ws_entities WHERE workspace_id = ? AND deleted_at IS NULL "
                         "GROUP BY status", (ws_id,)).fetchall()
        return {"id": ws_id, "name": m["name"], "role": m["role"], "seat_limit": m["seat_limit"], "members": n,
                "contacts": {r["status"]: r["n"] for r in e}}
    finally:
        conn.close()


def list_members(db_path, ws_id, email):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        return _rows(conn.execute("SELECT email, role, added_by, added_at FROM workspace_members "
                                  "WHERE workspace_id = ? AND removed_at IS NULL ORDER BY role, email",
                                  (ws_id,)).fetchall())
    finally:
        conn.close()


def add_member(db_path, ws_id, email, new_email, role="member"):
    """Workspace admin adds a colleague. Returns the new member's email (the
    HTTP layer then lets that email through Cloudflare Access)."""
    if role not in ROLES:
        raise WorkspaceError("role must be admin or member")
    new_email = _email(new_email)
    conn = db.connect(db_path)
    try:
        m = _member(conn, ws_id, email, role="admin")
        if conn.execute("SELECT 1 FROM workspace_members WHERE workspace_id = ? AND email = ? AND removed_at IS NULL",
                        (ws_id, new_email)).fetchone():
            raise WorkspaceError("already a member")
        n = conn.execute("SELECT count(*) AS n FROM workspace_members WHERE workspace_id = ? AND removed_at IS NULL",
                         (ws_id,)).fetchone()["n"]
        if n >= m["seat_limit"]:
            raise WorkspaceError(f"this workspace has all {m['seat_limit']} of its seats in use", 409)
        conn.execute("INSERT INTO workspace_members (workspace_id, email, role, added_by, added_at) VALUES (?, ?, ?, ?, ?)",
                     (ws_id, new_email, role, email, _now()))
        audit(conn, ws_id, email, "member_added", {"email": new_email, "role": role})
        conn.commit()
        return new_email
    finally:
        conn.close()


def remove_member(db_path, ws_id, email, member_email):
    member_email = (member_email or "").lower()
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email, role="admin")
        target = conn.execute("SELECT role FROM workspace_members WHERE workspace_id = ? AND email = ? "
                              "AND removed_at IS NULL", (ws_id, member_email)).fetchone()
        if target is None:
            raise WorkspaceError("Not Found", 404)
        if target["role"] == "admin":
            admins = conn.execute("SELECT count(*) AS n FROM workspace_members WHERE workspace_id = ? "
                                  "AND role = 'admin' AND removed_at IS NULL", (ws_id,)).fetchone()["n"]
            if admins <= 1:
                raise WorkspaceError("a workspace needs at least one admin", 409)
        conn.execute("UPDATE workspace_members SET removed_at = ? WHERE workspace_id = ? AND email = ? "
                     "AND removed_at IS NULL", (_now(), ws_id, member_email))
        audit(conn, ws_id, email, "member_removed", {"email": member_email})
        conn.commit()
    finally:
        conn.close()


def delete_workspace(db_path, ws_id, email):
    """Admin deletes the whole workspace: its contacts are erased at once
    (rows removed, not just hidden); the audit trail keeps only counts."""
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email, role="admin")
        n = conn.execute("SELECT count(*) AS n FROM ws_entities WHERE workspace_id = ?", (ws_id,)).fetchone()["n"]
        conn.execute("DELETE FROM ws_entities WHERE workspace_id = ?", (ws_id,))
        conn.execute("DELETE FROM ws_sources WHERE workspace_id = ?", (ws_id,))
        conn.execute("UPDATE workspace_members SET removed_at = ? WHERE workspace_id = ? AND removed_at IS NULL",
                     (_now(), ws_id))
        conn.execute("UPDATE workspaces SET deleted_at = ? WHERE id = ?", (_now(), ws_id))
        audit(conn, ws_id, email, "workspace_deleted", {"contacts_erased": n})
        conn.commit()
    finally:
        conn.close()


def audit_log(db_path, ws_id, email, limit=200):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email, role="admin")
        return _rows(conn.execute("SELECT email, action, detail, at FROM ws_audit WHERE workspace_id = ? "
                                  "ORDER BY id DESC LIMIT ?", (ws_id, int(limit))).fetchall())
    finally:
        conn.close()


# --- contact files -------------------------------------------------------------

def _clean(s, n=200):
    return re.sub(r"\s+", " ", (s or "")).strip()[:n]


def _pick(row, *names):
    low = {(k or "").strip().lower(): v for k, v in row.items()}
    for n in names:
        if low.get(n):
            return low[n]
    return ""


def parse_contacts(data: bytes, filename: str):
    """LinkedIn Connections.csv, Google / Outlook contact CSVs, or vCard ->
    [{"name", "company", "title"}]. Emails and phone numbers are not kept."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise WorkspaceError(f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", 413)
    text = data.decode("utf-8-sig", errors="replace")
    out = []
    if filename.lower().endswith((".vcf", ".vcard")) or text.lstrip().upper().startswith("BEGIN:VCARD"):
        for card in re.split(r"(?im)^BEGIN:VCARD\s*$", text)[1:]:
            fields = {}
            for line in re.sub(r"\r?\n[ \t]", "", card).splitlines():   # unfold continuation lines
                key, _, val = line.partition(":")
                fields.setdefault(key.split(";")[0].upper(), val)
            name = fields.get("FN") or " ".join(reversed([p for p in fields.get("N", "").split(";")[:2] if p]))
            out.append({"name": _clean(name), "company": _clean(fields.get("ORG", "").split(";")[0]),
                        "title": _clean(fields.get("TITLE", ""))})
    else:
        lines = text.splitlines()
        # LinkedIn puts a few "Notes:" lines above the header row
        start = next((i for i, l in enumerate(lines[:20])
                      if re.search(r"(?i)\b(first name|name|given name)\b", l) and "," in l), 0)
        for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
            first = _pick(row, "first name", "given name")
            last = _pick(row, "last name", "family name", "surname")
            name = f"{first} {last}".strip() or _pick(row, "name", "full name", "display name")
            out.append({"name": _clean(name),
                        "company": _clean(_pick(row, "company", "organization 1 - name", "organization name",
                                                "organization")),
                        "title": _clean(_pick(row, "position", "title", "job title", "organization 1 - title"))})
    out = [c for c in out if len(c["name"].split()) >= 2]
    if len(out) > MAX_CONTACT_ROWS:
        raise WorkspaceError(f"more than {MAX_CONTACT_ROWS:,} contacts in one file", 413)
    return out


def import_contacts(db_path, ws_id, email, filename, contacts, resolve):
    """Store uploaded contacts as workspace entities known by `email`.
    resolve(contact) -> (status, node or None, candidates list). Re-uploading
    a file with the same name replaces that member's previous upload of it."""
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        old = conn.execute("SELECT id FROM ws_sources WHERE workspace_id = ? AND uploaded_by = ? AND filename = ? "
                           "AND deleted_at IS NULL", (ws_id, email, filename)).fetchall()
        for r in old:
            conn.execute("DELETE FROM ws_entities WHERE workspace_id = ? AND source_id = ?", (ws_id, r["id"]))
            conn.execute("UPDATE ws_sources SET deleted_at = ? WHERE id = ?", (_now(), r["id"]))
        conn.execute("INSERT INTO ws_sources (workspace_id, kind, filename, uploaded_by, row_count, created_at) "
                     "VALUES (?, 'contacts', ?, ?, ?, ?)", (ws_id, filename, email, len(contacts), _now()))
        src = conn.execute("SELECT max(id) AS id FROM ws_sources WHERE workspace_id = ? AND uploaded_by = ?",
                           (ws_id, email)).fetchone()["id"]
        counts = {MATCHED: 0, AMBIGUOUS: 0, UNMATCHED: 0}
        seen = set()
        for c in contacts:
            key = (c["name"].lower(), c.get("company", "").lower())
            if key in seen:
                continue
            seen.add(key)
            status, node, cands = resolve(c)
            counts[status] += 1
            conn.execute("INSERT INTO ws_entities (workspace_id, source_id, known_by, name, entity_type, company, title, "
                         "node, status, candidates, created_at) VALUES (?, ?, ?, ?, 'PERSON', ?, ?, ?, ?, ?, ?)",
                         (ws_id, src, email, c["name"], c.get("company") or None, c.get("title") or None, node, status,
                          json.dumps(cands[:10]) if cands else None, _now()))
        audit(conn, ws_id, email, "contacts_uploaded", {"file": filename, "rows": len(contacts), **counts,
                                                        "replaced": len(old)})
        conn.commit()
        return {"source_id": src, "rows": len(contacts), **counts, "replaced_previous": bool(old)}
    finally:
        conn.close()


def list_contacts(db_path, ws_id, email, status=None, q=None, limit=500, offset=0):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        sql = ("SELECT id, name, company, title, node, status, candidates, known_by FROM ws_entities "
               "WHERE workspace_id = ? AND deleted_at IS NULL")
        args = [ws_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        if q:
            sql += " AND (lower(name) LIKE ? OR lower(coalesce(company, '')) LIKE ?)"
            args += [f"%{q.lower()}%"] * 2
        sql += " ORDER BY name, id LIMIT ? OFFSET ?"
        args += [int(limit), int(offset)]
        out = _rows(conn.execute(sql, args).fetchall())
        for r in out:
            r["candidates"] = json.loads(r["candidates"]) if r["candidates"] else []
        return out
    finally:
        conn.close()


def resolve_contact(db_path, ws_id, email, entity_id, node, is_node):
    """A member settles an ambiguous or wrong match: node = the chosen graph
    node, or None for "None of these" (stays private, unmatched)."""
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        r = conn.execute("SELECT id FROM ws_entities WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
                         (entity_id, ws_id)).fetchone()
        if r is None:
            raise WorkspaceError("Not Found", 404)
        if node is not None and not is_node(node):
            raise WorkspaceError("that name isn't in the database")
        conn.execute("UPDATE ws_entities SET node = ?, status = ?, candidates = NULL WHERE id = ? AND workspace_id = ?",
                     (node, MATCHED if node else UNMATCHED, entity_id, ws_id))
        audit(conn, ws_id, email, "contact_resolved", {"id": entity_id, "matched": bool(node)})
        conn.commit()
    finally:
        conn.close()


def delete_contact(db_path, ws_id, email, entity_id):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        r = conn.execute("SELECT id FROM ws_entities WHERE id = ? AND workspace_id = ?", (entity_id, ws_id)).fetchone()
        if r is None:
            raise WorkspaceError("Not Found", 404)
        conn.execute("DELETE FROM ws_entities WHERE id = ? AND workspace_id = ?", (entity_id, ws_id))
        audit(conn, ws_id, email, "contact_deleted", {"id": entity_id})
        conn.commit()
    finally:
        conn.close()


def list_sources(db_path, ws_id, email):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        return _rows(conn.execute("SELECT id, kind, filename, uploaded_by, row_count, created_at FROM ws_sources "
                                  "WHERE workspace_id = ? AND deleted_at IS NULL ORDER BY id DESC", (ws_id,)).fetchall())
    finally:
        conn.close()


def delete_source(db_path, ws_id, email, source_id):
    """Removes an upload and every contact it brought in. The uploader or an
    admin may do it."""
    conn = db.connect(db_path)
    try:
        m = _member(conn, ws_id, email)
        s = conn.execute("SELECT uploaded_by FROM ws_sources WHERE id = ? AND workspace_id = ? AND deleted_at IS NULL",
                         (source_id, ws_id)).fetchone()
        if s is None:
            raise WorkspaceError("Not Found", 404)
        if s["uploaded_by"] != email and m["role"] != "admin":
            raise WorkspaceError("only the uploader or an admin can remove this file", 403)
        n = conn.execute("SELECT count(*) AS n FROM ws_entities WHERE workspace_id = ? AND source_id = ?",
                         (ws_id, source_id)).fetchone()["n"]
        conn.execute("DELETE FROM ws_entities WHERE workspace_id = ? AND source_id = ?", (ws_id, source_id))
        conn.execute("UPDATE ws_sources SET deleted_at = ? WHERE id = ?", (_now(), source_id))
        audit(conn, ws_id, email, "source_deleted", {"id": source_id, "contacts_erased": n})
        conn.commit()
        return n
    finally:
        conn.close()


def matched_contacts(db_path, ws_id, email):
    """[(node, known_by)] for path searches -- matched contacts only."""
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        return [(r["node"], r["known_by"]) for r in conn.execute(
            "SELECT node, known_by FROM ws_entities WHERE workspace_id = ? AND status = ? AND node IS NOT NULL "
            "AND deleted_at IS NULL", (ws_id, MATCHED)).fetchall()]
    finally:
        conn.close()


def export_csv(db_path, ws_id, email):
    conn = db.connect(db_path)
    try:
        _member(conn, ws_id, email)
        rows = conn.execute("SELECT name, company, title, node, status, known_by FROM ws_entities "
                            "WHERE workspace_id = ? AND deleted_at IS NULL ORDER BY name", (ws_id,)).fetchall()
        audit(conn, ws_id, email, "exported", {"rows": len(rows)})
        conn.commit()
    finally:
        conn.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "company", "title", "matched_to", "status", "added_by"])
    for r in rows:
        w.writerow([r["name"], r["company"] or "", r["title"] or "", r["node"] or "", r["status"], r["known_by"]])
    return buf.getvalue()
