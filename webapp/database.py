import os
import sqlite3
import json

try:
    from test_department import init_test_department_db
except ImportError:
    from webapp.test_department import init_test_department_db

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "test_department.db")


def init_db(db_path: str = DB_PATH):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    # Check if user_submissions.db exists and needs migration
    old_db_path = os.path.join(os.path.dirname(db_path), "user_submissions.db")
    has_old_db = os.path.exists(old_db_path)
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    # Create unified service_items table
    c.execute("""
        CREATE TABLE IF NOT EXISTS service_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_key TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_url TEXT,
            user_email TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    
    # Run migration if user_submissions.db is found
    if has_old_db:
        try:
            old_conn = sqlite3.connect(old_db_path)
            old_cur = old_conn.cursor()
            
            # Check if claims table exists in old db
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
            
            # Check if disputes table exists in old db
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
            
            # Rename old database
            os.rename(old_db_path, old_db_path + ".migrated")
            print("[migration] Migrated user_submissions.db to test_department.db successfully.")
        except Exception as e:
            print(f"[migration] Error during database migration: {e}")
            
    conn.close()
    
    # Initialize tester tables in the same DB file
    init_test_department_db(db_path)

def init_legal_compliance_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS legal_obligations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            route TEXT,
            latency_ms REAL,
            status_code INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS anonymous_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            metadata TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
