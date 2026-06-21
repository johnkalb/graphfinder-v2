import sqlite3
import json
import os

class DBManager:
    def __init__(self, db_path="data/pipeline_cache.db"):
        self.db_path = db_path
        # Ensure the directory for the database exists
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        # Create cache table for raw SEC payloads
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sec_cache (
                cik TEXT PRIMARY KEY,
                data TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Create parsed relationships table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS relationships (
                source_id TEXT,
                source_name TEXT,
                source_type TEXT, -- 'PERSON' or 'COMPANY'
                target_id TEXT,
                target_name TEXT,
                target_type TEXT,
                relation_type TEXT, -- 'DIRECTOR', 'OFFICER', '10% OWNER', etc.
                source_data TEXT, -- 'SEC' or 'WIKIPEDIA'
                UNIQUE(source_name, target_name, relation_type)
            )
        """)
        conn.commit()
        conn.close()

    def get_sec_cache(self, cik):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT data FROM sec_cache WHERE cik = ?", (cik,))
        row = cursor.fetchone()
        conn.close()
        return json.loads(row[0]) if row else None

    def save_sec_cache(self, cik, data):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO sec_cache (cik, data) VALUES (?, ?)", (cik, json.dumps(data)))
        conn.commit()
        conn.close()

    def add_relationship(self, src_id, src_name, src_type, tgt_id, tgt_name, tgt_type, relation, source_data):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO relationships 
                (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                src_id, 
                src_name.strip() if src_name else "", 
                src_type, 
                tgt_id, 
                tgt_name.strip() if tgt_name else "", 
                tgt_type, 
                relation, 
                source_data
            ))
            conn.commit()
        except sqlite3.Error:
            pass
        finally:
            conn.close()
