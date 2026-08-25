import os
import json

try:
    import db
    from test_department import init_test_department_db
except ImportError:
    from webapp import db
    from webapp.test_department import init_test_department_db

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "test_department.db")

_PK = "SERIAL PRIMARY KEY" if db.IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def init_db(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = db.connect(db_path)
    c = conn.cursor()

    # Create unified service_items table
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS service_items (
            id {_PK},
            item_type TEXT NOT NULL,  -- 'suggestion', 'dispute', 'feedback', 'legal_review'
            status TEXT DEFAULT 'new',  -- 'new', 'needs_review', 'approved', 'rejected', 'resolved', 'archived'
            priority TEXT DEFAULT 'normal',  -- 'low', 'normal', 'high', 'urgent'
            subject TEXT,
            body TEXT,
            submitter_email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_by TEXT,
            reviewed_at TIMESTAMP,
            resolution_note TEXT,
            edge_key TEXT,
            metadata TEXT  -- JSON blob for type-specific fields
        )
    """)

    # Create legacy claims and disputes tables for backwards-compatibility in tests
    c.execute(f"""
        CREATE TABLE IF NOT EXISTS claims (
            id {_PK},
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            source_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            snippet TEXT NOT NULL,
            user_email TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    c.execute(f"""
        CREATE TABLE IF NOT EXISTS disputes (
            id {_PK},
            edge_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_url TEXT,
            user_email TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()

    # One-time migration from the legacy user_submissions.db -- only makes
    # sense against ephemeral SQLite, where both the source (baked into the
    # git-tracked image) and destination reset together on every boot. On
    # Postgres the destination is persistent but the source still reappears
    # fresh from the image on every deploy, which would insert duplicate
    # "Test Person A"/"Test Person B" rows forever since this does plain
    # INSERTs with no dedupe key. Skip it entirely once on Postgres.
    if not db.IS_POSTGRES:
        old_db_path = os.path.join(os.path.dirname(db_path), "user_submissions.db")
        if os.path.exists(old_db_path):
            try:
                import sqlite3
                old_conn = sqlite3.connect(old_db_path)
                old_cur = old_conn.cursor()

                old_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='claims'")
                if old_cur.fetchone():
                    old_cur.execute("SELECT * FROM claims")
                    for row in old_cur.fetchall():
                        status_mapped = 'new' if row[8] == 'pending' else row[8]
                        meta = json.dumps({
                            "subject": row[1],
                            "predicate": row[2],
                            "object": row[3],
                            "source_name": row[4],
                            "source_url": row[5],
                            "snippet": row[6]
                        })
                        c.execute("""
                            INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, created_at, metadata)
                            VALUES ('suggestion', ?, 'normal', ?, ?, ?, ?, ?)
                        """, (status_mapped, f"Suggestion: {row[1]} {row[2]} {row[3]}", row[6], row[7], row[9], meta))

                old_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='disputes'")
                if old_cur.fetchone():
                    old_cur.execute("SELECT * FROM disputes")
                    for row in old_cur.fetchall():
                        status_mapped = 'new' if row[5] == 'pending' else row[5]
                        meta = json.dumps({
                            "edge_key": row[1],
                            "reason": row[2],
                            "source_url": row[3]
                        })
                        c.execute("""
                            INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, created_at, edge_key, metadata)
                            VALUES ('dispute', ?, 'normal', ?, ?, ?, ?, ?, ?)
                        """, (status_mapped, f"Dispute: {row[1]}", row[2], row[4], row[6], row[1], meta))

                old_conn.close()
                conn.commit()

                os.rename(old_db_path, old_db_path + ".migrated")
                print("[migration] Migrated user_submissions.db to test_department.db successfully.")
            except Exception as e:
                print(f"[migration] Error during database migration: {e}")

    conn.close()

    # Initialize tester tables in the same DB
    init_test_department_db(db_path)

def init_legal_compliance_db(db_path: str):
    conn = db.connect(db_path)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS legal_obligations (
            id {_PK},
            source_url TEXT NOT NULL,
            terms_hash TEXT NOT NULL,
            extracted_obligations TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            valid_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            raw_text TEXT
        )
    """)
    conn.commit()
    conn.close()

def init_ops_db(db_path: str):
    conn = db.connect(db_path)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS request_logs (
            id {_PK},
            route TEXT,
            latency_ms REAL,
            status_code INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS anonymous_events (
            id {_PK},
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            metadata TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def init_mentioned_with_cache_db(db_path: str):
    """Backs the last-resort news-co-mention fallback (see
    webapp/mentioned_with_fallback.py): mentioned_with_cache persists each
    (pair -> classification) result so a live GDELT search + Haiku call
    only ever happens once per pair, and llm_spend_tracker is the hard
    monthly cap on those Haiku calls. Both must be DB-backed (not the
    in-process dict pattern _narrative_cache uses) -- App Platform
    redeploys on every push to scoring-model, and an in-memory cap would
    silently reset every time, defeating the point of a cap."""
    conn = db.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mentioned_with_cache (
            pair_key TEXT PRIMARY KEY,
            name_a TEXT,
            name_b TEXT,
            found INTEGER,
            category TEXT,
            reason TEXT,
            source_url TEXT,
            article_title TEXT,
            article_date TEXT,
            classified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_spend_tracker (
            period TEXT PRIMARY KEY,
            call_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
    init_ops_db(os.path.join(os.path.dirname(DB_PATH), "ops_metrics.db"))
    print("Ops database initialized.")
    init_legal_compliance_db(os.path.join(os.path.dirname(DB_PATH), "legal_compliance.db"))
    print("Legal compliance database initialized.")
    init_mentioned_with_cache_db(os.path.join(os.path.dirname(DB_PATH), "mentioned_with_cache.db"))
    print("Mentioned-with fallback cache database initialized.")
