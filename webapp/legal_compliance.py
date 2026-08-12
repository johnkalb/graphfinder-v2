import hashlib
import json
from typing import Optional
from pathlib import Path

try:
    import db
except ImportError:
    from webapp import db

# Placeholder for path to legal DB
def get_legal_db_path(data_dir: Path) -> str:
    return str(data_dir / "legal_compliance.db")

def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def register_obligation(
    db_path: str,
    source_url: str,
    raw_text: str,
    extracted_obligations: dict
) -> int:
    conn = db.connect(db_path)
    cur = conn.cursor()
    terms_hash = _hash_text(raw_text)

    cur.execute("""
        INSERT INTO legal_obligations
        (source_url, terms_hash, extracted_obligations, raw_text)
        VALUES (?, ?, ?, ?)
        RETURNING id
    """, (source_url, terms_hash, json.dumps(extracted_obligations), raw_text))

    obligation_id = cur.fetchone()["id"]
    conn.commit()
    conn.close()
    return obligation_id

def check_for_changes(db_path: str, source_url: str, current_raw_text: str) -> Optional[str]:
    conn = db.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        SELECT terms_hash FROM legal_obligations 
        WHERE source_url = ? 
        ORDER BY valid_from DESC LIMIT 1
    """, (source_url,))
    row = cur.fetchone()
    conn.close()
    
    if not row:
        return "new_source"
    
    if row["terms_hash"] != _hash_text(current_raw_text):
        return "terms_changed"
    
    return None
