import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

try:
    import db
except ImportError:
    from webapp import db

_PK = "SERIAL PRIMARY KEY" if db.IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

WEB_ACTIVITY_EVENTS = {
    "page_view",
    "search",
    "path_found",
    "path_not_found",
    "path_error",
    "unknown_entity",
}
MEANINGFUL_USAGE_EVENTS = {
    "search",
    "path_found",
    "path_not_found",
}
CASE_OPEN_CATEGORIES = {"performance", "bug", "confusion", "trust", "coverage"}


def _db_connect(db_path: str):
    return db.connect(db_path)


def _normalize_email(email: Optional[str]) -> Optional[str]:
    if not email:
        return None
    value = email.strip().lower()
    return value or None


def _coerce_timestamp(value: Optional[Any]) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return datetime.now(timezone.utc).isoformat()
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
        except ValueError:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.isoformat()
    raise TypeError(f"Unsupported timestamp value: {value!r}")


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def init_test_department_db(db_path: str) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = _db_connect(db_path)
    cur = conn.cursor()
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS testers (
            id {_PK},
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            invited_by_email TEXT,
            invited_by_name TEXT,
            invite_source TEXT,
            invite_subject TEXT,
            invite_body TEXT,
            invite_delivery_status TEXT,
            invite_message_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if db.IS_POSTGRES:
        # Postgres supports IF NOT EXISTS on ADD COLUMN natively -- no need
        # to introspect first.
        cur.execute("ALTER TABLE testers ADD COLUMN IF NOT EXISTS invite_delivery_status TEXT")
        cur.execute("ALTER TABLE testers ADD COLUMN IF NOT EXISTS invite_message_id TEXT")
    else:
        # Ensure columns exist on old database schema
        cur.execute("PRAGMA table_info(testers)")
        cols = [r["name"] for r in cur.fetchall()]
        if "invite_delivery_status" not in cols:
            cur.execute("ALTER TABLE testers ADD COLUMN invite_delivery_status TEXT")
        if "invite_message_id" not in cols:
            cur.execute("ALTER TABLE testers ADD COLUMN invite_message_id TEXT")

    cur.execute(f"""
        CREATE TABLE IF NOT EXISTS service_items (
            id {_PK},
            item_type TEXT NOT NULL,
            status TEXT DEFAULT 'new',
            priority TEXT DEFAULT 'normal',
            subject TEXT,
            body TEXT,
            submitter_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_by TEXT,
            reviewed_at TIMESTAMP,
            resolution_note TEXT,
            edge_key TEXT,
            metadata TEXT
        )
    """)
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tester_events (
            id {_PK},
            tester_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_at TEXT NOT NULL,
            is_meaningful INTEGER NOT NULL DEFAULT 0,
            source TEXT,
            metadata_json TEXT,
            FOREIGN KEY (tester_id) REFERENCES testers(id)
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tester_feedback (
            id {_PK},
            tester_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            subject TEXT,
            feedback_text TEXT NOT NULL,
            category TEXT,
            raw_payload TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (tester_id) REFERENCES testers(id)
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tester_notes (
            id {_PK},
            tester_id INTEGER NOT NULL,
            author_email TEXT,
            note_text TEXT NOT NULL,
            source TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (tester_id) REFERENCES testers(id)
        )
        """
    )
    cur.execute(
        f"""
        CREATE TABLE IF NOT EXISTS tester_cases (
            id {_PK},
            tester_id INTEGER NOT NULL,
            case_type TEXT NOT NULL,
            summary TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'open',
            evidence_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            resolved_at TEXT,
            FOREIGN KEY (tester_id) REFERENCES testers(id)
        )
        """
    )
    conn.commit()
    conn.close()


def _ensure_tester(
    conn: sqlite3.Connection,
    tester_email: str,
    *,
    name: Optional[str] = None,
    invited_by_email: Optional[str] = None,
    invited_by_name: Optional[str] = None,
    invite_source: Optional[str] = None,
    invite_subject: Optional[str] = None,
    invite_body: Optional[str] = None,
    created_at: Optional[Any] = None,
) -> int:
    email = _normalize_email(tester_email)
    if not email:
        raise ValueError("tester_email is required")
    now_iso = _coerce_timestamp(created_at)
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM testers WHERE email = ?", (email,)).fetchone()
    if row:
        cur.execute(
            """
            UPDATE testers
            SET name = COALESCE(?, name),
                invited_by_email = COALESCE(?, invited_by_email),
                invited_by_name = COALESCE(?, invited_by_name),
                invite_source = COALESCE(?, invite_source),
                invite_subject = COALESCE(?, invite_subject),
                invite_body = COALESCE(?, invite_body),
                updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip() if isinstance(name, str) and name.strip() else None,
                _normalize_email(invited_by_email),
                invited_by_name.strip() if isinstance(invited_by_name, str) and invited_by_name.strip() else None,
                invite_source.strip() if isinstance(invite_source, str) and invite_source.strip() else None,
                invite_subject.strip() if isinstance(invite_subject, str) and invite_subject.strip() else None,
                invite_body.strip() if isinstance(invite_body, str) and invite_body.strip() else None,
                now_iso,
                row["id"],
            ),
        )
        return int(row["id"])

    cur.execute(
        """
        INSERT INTO testers (
            email, name, invited_by_email, invited_by_name, invite_source,
            invite_subject, invite_body, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            email,
            name.strip() if isinstance(name, str) and name.strip() else None,
            _normalize_email(invited_by_email),
            invited_by_name.strip() if isinstance(invited_by_name, str) and invited_by_name.strip() else None,
            invite_source.strip() if isinstance(invite_source, str) and invite_source.strip() else None,
            invite_subject.strip() if isinstance(invite_subject, str) and invite_subject.strip() else None,
            invite_body.strip() if isinstance(invite_body, str) and invite_body.strip() else None,
            now_iso,
            now_iso,
        ),
    )
    return int(cur.fetchone()["id"])


def _insert_event(
    conn: sqlite3.Connection,
    tester_id: int,
    *,
    event_type: str,
    event_at: Optional[Any] = None,
    is_meaningful: bool = False,
    source: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tester_events (tester_id, event_type, event_at, is_meaningful, source, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            tester_id,
            event_type,
            _coerce_timestamp(event_at),
            1 if is_meaningful else 0,
            source,
            json.dumps(metadata or {}, sort_keys=True),
        ),
    )
    return int(cur.fetchone()["id"])


def record_invitation(
    db_path: str,
    *,
    invitee_email: str,
    invitee_name: Optional[str] = None,
    inviter_email: Optional[str] = None,
    inviter_name: Optional[str] = None,
    source: str = "bcc",
    subject: Optional[str] = None,
    body: Optional[str] = None,
    sent_at: Optional[Any] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    init_test_department_db(db_path)
    conn = _db_connect(db_path)
    tester_id = _ensure_tester(
        conn,
        invitee_email,
        name=invitee_name,
        invited_by_email=inviter_email,
        invited_by_name=inviter_name,
        invite_source=source,
        invite_subject=subject,
        invite_body=body,
        created_at=sent_at,
    )
    event_id = _insert_event(
        conn,
        tester_id,
        event_type="invite_bcc",
        event_at=sent_at,
        is_meaningful=False,
        source=source,
        metadata=metadata,
    )
    conn.commit()
    conn.close()
    return event_id


def record_usage_event(
    db_path: str,
    *,
    tester_email: str,
    event_type: str,
    event_at: Optional[Any] = None,
    name: Optional[str] = None,
    source: str = "web",
    metadata: Optional[dict[str, Any]] = None,
    is_meaningful: Optional[bool] = None,
    diagnostic_results: Optional[dict[str, Any]] = None,
) -> int:
    init_test_department_db(db_path)
    conn = _db_connect(db_path)
    tester_id = _ensure_tester(conn, tester_email, name=name, created_at=event_at)
    if is_meaningful is None:
        is_meaningful = event_type in MEANINGFUL_USAGE_EVENTS
    
    # Merge diagnostic results into metadata if provided
    final_metadata = metadata or {}
    if diagnostic_results:
        final_metadata["bineval"] = diagnostic_results

    event_id = _insert_event(
        conn,
        tester_id,
        event_type=event_type,
        event_at=event_at,
        is_meaningful=is_meaningful,
        source=source,
        metadata=final_metadata,
    )
    if event_type in {"path_error", "frontend_error", "api_error", "unknown_entity"}:
        _open_case_if_needed(
            conn,
            tester_id,
            case_type="coverage" if event_type == "unknown_entity" else "bug",
            summary=f"Observed {event_type.replace('_', ' ')} for tester.",
            evidence={"event_type": event_type, "metadata": metadata or {}},
            created_at=event_at,
        )
    conn.commit()
    conn.close()
    return event_id


def classify_feedback(feedback_text: str) -> str:
    text = (feedback_text or "").lower()
    if any(token in text for token in ["slow", "slower", "latency", "timeout", "timed out", "hung"]):
        return "performance"
    if any(token in text for token in ["error", "bug", "crash", "exception", "broke", "failed"]):
        return "bug"
    if any(token in text for token in ["confus", "didn't understand", "did not understand", "not sure", "unclear"]):
        return "confusion"
    if any(token in text for token in ["trust", "confidence", "believe", "skeptical", "wrong result"]):
        return "trust"
    if any(token in text for token in ["not found", "missing", "doesn't exist", "doesn't have", "can't find"]):
        return "coverage"
    if any(token in text for token in ["busy", "no time", "too busy", "later", "next week"]):
        return "availability"
    if any(token in text for token in ["love", "great", "helpful", "useful", "cool"]):
        return "praise"
    return "general"


def _open_case_if_needed(
    conn: sqlite3.Connection,
    tester_id: int,
    *,
    case_type: str,
    summary: str,
    evidence: Optional[dict[str, Any]] = None,
    created_at: Optional[Any] = None,
    severity: str = "medium",
) -> int:
    cur = conn.cursor()
    existing = cur.execute(
        "SELECT id FROM tester_cases WHERE tester_id = ? AND case_type = ? AND status = 'open' ORDER BY id DESC LIMIT 1",
        (tester_id, case_type),
    ).fetchone()
    now_iso = _coerce_timestamp(created_at)
    if existing:
        cur.execute(
            "UPDATE tester_cases SET summary = ?, evidence_json = ?, updated_at = ? WHERE id = ?",
            (summary, json.dumps(evidence or {}, sort_keys=True), now_iso, int(existing["id"])),
        )
        return int(existing["id"])

    cur.execute(
        """
        INSERT INTO tester_cases (tester_id, case_type, summary, severity, status, evidence_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        RETURNING id
        """,
        (
            tester_id,
            case_type,
            summary,
            severity,
            json.dumps(evidence or {}, sort_keys=True),
            now_iso,
            now_iso,
        ),
    )
    return int(cur.fetchone()["id"])


def add_feedback(
    db_path: str,
    *,
    tester_email: str,
    feedback_text: str,
    source: str = "forwarded_email",
    subject: Optional[str] = None,
    received_at: Optional[Any] = None,
    category: Optional[str] = None,
    raw_payload: Optional[str] = None,
) -> int:
    init_test_department_db(db_path)
    category = category or classify_feedback(feedback_text)
    conn = _db_connect(db_path)
    tester_id = _ensure_tester(conn, tester_email, created_at=received_at)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tester_feedback (tester_id, source, subject, feedback_text, category, raw_payload, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            tester_id,
            source,
            subject.strip() if isinstance(subject, str) and subject.strip() else None,
            feedback_text.strip(),
            category,
            raw_payload,
            _coerce_timestamp(received_at),
        ),
    )
    feedback_id = int(cur.fetchone()["id"])
    _insert_event(
        conn,
        tester_id,
        event_type="feedback_received",
        event_at=received_at,
        is_meaningful=False,
        source=source,
        metadata={"category": category, "subject": subject},
    )

    # Write to consolidated service_items table
    priority = "high" if category in CASE_OPEN_CATEGORIES else "normal"
    meta = json.dumps({"category": category, "raw_payload": raw_payload})
    cur.execute(
        """
        INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, created_at, metadata)
        VALUES ('feedback', 'new', ?, ?, ?, ?, ?, ?)
        """,
        (
            priority,
            subject.strip() if isinstance(subject, str) and subject.strip() else f"Feedback from {tester_email}",
            feedback_text.strip(),
            tester_email,
            _coerce_timestamp(received_at),
            meta
        )
    )

    if category in CASE_OPEN_CATEGORIES:
        _open_case_if_needed(
            conn,
            tester_id,
            case_type=category,
            summary=f"Tester reported {category} issue: {feedback_text.strip()[:160]}",
            evidence={"feedback_id": feedback_id, "subject": subject, "source": source},
            created_at=received_at,
        )
    conn.commit()
    conn.close()
    return feedback_id


def add_note(
    db_path: str,
    *,
    tester_email: str,
    note_text: str,
    author_email: Optional[str] = None,
    source: str = "conversation_note",
    noted_at: Optional[Any] = None,
) -> int:
    init_test_department_db(db_path)
    conn = _db_connect(db_path)
    tester_id = _ensure_tester(conn, tester_email, created_at=noted_at)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO tester_notes (tester_id, author_email, note_text, source, created_at)
        VALUES (?, ?, ?, ?, ?)
        RETURNING id
        """,
        (
            tester_id,
            _normalize_email(author_email),
            note_text.strip(),
            source,
            _coerce_timestamp(noted_at),
        ),
    )
    note_id = int(cur.fetchone()["id"])
    _insert_event(
        conn,
        tester_id,
        event_type="note_added",
        event_at=noted_at,
        is_meaningful=False,
        source=source,
        metadata={"author_email": _normalize_email(author_email)},
    )
    conn.commit()
    conn.close()
    return note_id


def _recommended_action(status: str) -> str:
    return {
        "invited": "Follow up on the invitation if they have not activated within a week.",
        "activated": "Encourage a first real search and ask what they want to learn.",
        "first_use": "Encourage a second visit and ask what they hoped to find.",
        "returned": "Ask what would make the product worth using weekly.",
        "frequent_user": "Thank them, learn their recurring workflow, and protect the habit.",
        "blocked": "Follow up directly, capture exact failure details, and track to resolution.",
        "stalled": "Send a personal nudge and ask whether they were confused, busy, or blocked.",
        "dormant": "Decide whether to re-engage with a concrete use case or archive them for now.",
    }[status]


def _load_rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    return list(conn.execute(query, params).fetchall())


def _derive_rollup(conn: sqlite3.Connection, tester_id: int, *, now: Optional[Any] = None) -> dict[str, Any]:
    tester = conn.execute("SELECT * FROM testers WHERE id = ?", (tester_id,)).fetchone()
    events = _load_rows(conn, "SELECT * FROM tester_events WHERE tester_id = ? ORDER BY event_at, id", (tester_id,))
    feedback = _load_rows(conn, "SELECT * FROM tester_feedback WHERE tester_id = ? ORDER BY created_at DESC, id DESC", (tester_id,))
    notes = _load_rows(conn, "SELECT * FROM tester_notes WHERE tester_id = ? ORDER BY created_at DESC, id DESC", (tester_id,))
    cases = _load_rows(conn, "SELECT * FROM tester_cases WHERE tester_id = ? ORDER BY created_at DESC, id DESC", (tester_id,))
    open_cases = [row for row in cases if row["status"] == "open"]

    activation_at = None
    meaningful_events: list[sqlite3.Row] = []
    last_activity_at = None
    invite_events = 0
    for row in events:
        ts = row["event_at"]
        if row["event_type"] == "invite_bcc":
            invite_events += 1
        else:
            last_activity_at = ts
        if activation_at is None and row["event_type"] in WEB_ACTIVITY_EVENTS:
            activation_at = ts
        if row["event_type"] in MEANINGFUL_USAGE_EVENTS:
            meaningful_events.append(row)

    first_use_at = meaningful_events[0]["event_at"] if meaningful_events else None
    meaningful_days: list[str] = []
    returned_at = None
    frequent_user_at = None
    for row in meaningful_events:
        day = row["event_at"][:10]
        if day not in meaningful_days:
            meaningful_days.append(day)
            if len(meaningful_days) == 2 and returned_at is None:
                returned_at = row["event_at"]
            if len(meaningful_days) == 3 and frequent_user_at is None:
                first_day = _parse_timestamp(meaningful_events[0]["event_at"])
                third_day = _parse_timestamp(row["event_at"])
                if first_day and third_day and (third_day - first_day) <= timedelta(days=21):
                    frequent_user_at = row["event_at"]

    if now is None:
        fallback_now = last_activity_at or first_use_at or activation_at or tester["updated_at"]
        now_dt = _parse_timestamp(fallback_now)
    else:
        now_dt = _parse_timestamp(_coerce_timestamp(now))
    last_activity_dt = _parse_timestamp(last_activity_at)
    last_meaningful_dt = _parse_timestamp(meaningful_events[-1]["event_at"]) if meaningful_events else None

    if open_cases:
        status = "blocked"
    elif frequent_user_at:
        status = "frequent_user"
    elif returned_at:
        status = "returned"
    elif first_use_at:
        gap = (now_dt - last_meaningful_dt).days if now_dt and last_meaningful_dt else 0
        if gap >= 30:
            status = "dormant"
        elif gap >= 14:
            status = "stalled"
        else:
            status = "first_use"
    elif activation_at:
        gap = (now_dt - last_activity_dt).days if now_dt and last_activity_dt else 0
        if gap >= 30:
            status = "dormant"
        elif gap >= 7:
            status = "stalled"
        else:
            status = "activated"
    else:
        status = "invited"

    likely_issue_category = None
    if open_cases:
        likely_issue_category = open_cases[0]["case_type"]
    elif feedback:
        likely_issue_category = feedback[0]["category"]

    return {
        "id": int(tester["id"]),
        "email": tester["email"],
        "name": tester["name"],
        "invited_by_email": tester["invited_by_email"],
        "invited_by_name": tester["invited_by_name"],
        "invite_source": tester["invite_source"],
        "invite_subject": tester["invite_subject"],
        "status": status,
        "invite_count": invite_events,
        "activation_at": activation_at,
        "first_use_at": first_use_at,
        "returned_at": returned_at,
        "frequent_user_at": frequent_user_at,
        "last_activity_at": last_activity_at,
        "meaningful_days": len(meaningful_days),
        "feedback_count": len(feedback),
        "note_count": len(notes),
        "open_case_count": len(open_cases),
        "likely_issue_category": likely_issue_category,
        "recommended_next_action": _recommended_action(status),
        "open_cases": [
            {
                "id": int(case["id"]),
                "case_type": case["case_type"],
                "summary": case["summary"],
                "status": case["status"],
                "severity": case["severity"],
                "created_at": case["created_at"],
                "updated_at": case["updated_at"],
            }
            for case in open_cases
        ],
    }


def get_tester_snapshot(db_path: str, tester_email: str, *, now: Optional[Any] = None) -> dict[str, Any]:
    init_test_department_db(db_path)
    conn = _db_connect(db_path)
    row = conn.execute("SELECT id FROM testers WHERE email = ?", (_normalize_email(tester_email),)).fetchone()
    if not row:
        conn.close()
        raise KeyError(f"Unknown tester: {tester_email}")
    result = _derive_rollup(conn, int(row["id"]), now=now)
    conn.close()
    return result


def list_attention_queue(db_path: str, *, now: Optional[Any] = None) -> list[dict[str, Any]]:
    init_test_department_db(db_path)
    conn = _db_connect(db_path)
    ids = [int(row["id"]) for row in conn.execute("SELECT id FROM testers ORDER BY id").fetchall()]
    items = [_derive_rollup(conn, tester_id, now=now) for tester_id in ids]
    conn.close()
    priority = {
        "blocked": 0,
        "stalled": 1,
        "activated": 2,
        "invited": 3,
        "dormant": 4,
        "first_use": 5,
        "returned": 6,
        "frequent_user": 7,
    }
    return sorted(items, key=lambda item: (priority[item["status"]], item["email"]))
