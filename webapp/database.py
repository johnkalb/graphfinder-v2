import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "user_submissions.db")

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 1. Claims table (for user-submitted connections)
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
            status TEXT DEFAULT 'pending', -- pending, approved, rejected
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # 2. Disputes table (for user-submitted corrections/disputes on existing edges)
    c.execute("""
        CREATE TABLE IF NOT EXISTS disputes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            edge_key TEXT NOT NULL, -- e.g. "donald trump|bill clinton|MENTIONED_WITH"
            reason TEXT NOT NULL,
            source_url TEXT,
            user_email TEXT,
            status TEXT DEFAULT 'pending', -- pending, reviewed, resolved, ignored
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database initialized successfully at:", DB_PATH)
