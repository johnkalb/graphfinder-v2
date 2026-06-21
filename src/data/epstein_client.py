"""EpsteinClient: Ingest Epstein House Committee data into the social graph."""
import csv
import os
import sqlite3
from src.utils.logger import logger

EPSTEIN_DIR = r"C:\Users\johnk\epstein_data"
PERSONS_CSV = os.path.join(EPSTEIN_DIR, "epstein_persons.csv")
GRAPH_DB = os.path.join(EPSTEIN_DIR, "graph_canonical.db")

# Maps CSV relationship types to graph relation types
RELATIONSHIP_MAP = {
    "EMPLOYED": "EMPLOYED_BY",
    "REPRESENTED": "REPRESENTED_BY",
    "COMMUNICATED": "COMMUNICATED_WITH",
    "MET": "MET_WITH",
    "PAID": "PAID_BY",
    "ASSOCIATED": "ASSOCIATED_WITH",
}

class EpsteinClient:
    def __init__(self):
        self.pipeline_db = None

    def _db(self):
        """Lazy-init the pipeline DB connection for cross-referencing."""
        if self.pipeline_db is None:
            from src.data.db_manager import DBManager
            from src.config import DB_PATH
            self.pipeline_db = DBManager(DB_PATH)
        return self.pipeline_db

    def load_persons_csv(self):
        """Parse epstein_persons.csv and return list of dicts."""
        results = []
        with open(PERSONS_CSV, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get("name", "").strip()
                if not name:
                    continue
                epstein_rel = row.get("epstein_relationship", "").strip()
                top_rel = row.get("relationship_to_top_person", "").strip()
                top_person = row.get("top_co_occurring_person", "").strip()
                rank = row.get("rank", "")
                results.append({
                    "name": name,
                    "epstein_relationship": epstein_rel,
                    "relationship_to_top_person": top_rel,
                    "top_co_occurring_person": top_person,
                    "rank": rank,
                })
        logger.info(f"Loaded {len(results)} persons from epstein_persons.csv")
        return results

    def save_csv_relationships(self, db_manager, persons):
        """Insert Epstein CSV person relationships into the pipeline DB."""
        count = 0
        
        for p in persons:
            name = p["name"]
            epstein_rel = p["epstein_relationship"]
            top_person = p["top_co_occurring_person"]
            top_rel = p["relationship_to_top_person"]
            
            # Skip Jeffrey Epstein himself
            if "jeffrey epstein" in name.lower():
                continue
            
            # 1. Epstein relationship (person -> Epstein)
            if epstein_rel and epstein_rel in RELATIONSHIP_MAP:
                mapped_rel = RELATIONSHIP_MAP[epstein_rel]
                try:
                    db_manager.add_relationship(
                        src_id=None, src_name=name, src_type="PERSON",
                        tgt_id=None, tgt_name="Jeffrey Epstein", tgt_type="PERSON",
                        relation=mapped_rel, source_data="EPSTEIN_COMMITTEE"
                    )
                    count += 1
                except Exception:
                    pass
            
            # 2. Top co-occurring relationship (person -> top_person)
            if top_person and top_rel and top_rel in RELATIONSHIP_MAP:
                mapped_rel = RELATIONSHIP_MAP[top_rel]
                try:
                    db_manager.add_relationship(
                        src_id=None, src_name=name, src_type="PERSON",
                        tgt_id=None, tgt_name=top_person, tgt_type="PERSON",
                        relation=mapped_rel, source_data="EPSTEIN_COMMITTEE"
                    )
                    count += 1
                except Exception:
                    pass
        
        logger.info(f"Inserted {count} CSV person relationships")
        return count

    def load_graph_db_edges(self, min_confidence=0.9):
        """Load edges from graph_canonical.db with optional confidence filter."""
        if not os.path.exists(GRAPH_DB):
            logger.error(f"graph_canonical.db not found at {GRAPH_DB}")
            return []
        
        conn = sqlite3.connect(GRAPH_DB)
        cursor = conn.cursor()
        
        # Skip edges where both ends are locations
        query = """
            SELECT e.edge_type, e.source_label, e.target_label, e.confidence,
                   sn.node_type as src_type, tn.node_type as tgt_type
            FROM edges e
            JOIN nodes sn ON e.source_node_id = sn.node_id
            JOIN nodes tn ON e.target_node_id = tn.node_id
            WHERE e.confidence >= ?
              AND NOT (sn.node_type = 'Location' AND tn.node_type = 'Location')
            ORDER BY e.confidence DESC
        """
        cursor.execute(query, (min_confidence,))
        rows = cursor.fetchall()
        conn.close()
        
        # Map type labels
        TYPE_MAP = {
            "Person": "PERSON", "Organization": "ORGANIZATION",
            "Location": "LOCATION", "Unknown": "UNKNOWN"
        }
        
        edges = []
        for edge_type, src_label, tgt_label, confidence, src_type, tgt_type in rows:
            src_type_mapped = TYPE_MAP.get(src_type, "UNKNOWN")
            tgt_type_mapped = TYPE_MAP.get(tgt_type, "UNKNOWN")
            edges.append({
                "edge_type": edge_type,
                "source_label": src_label,
                "target_label": tgt_label,
                "source_type": src_type_mapped,
                "target_type": tgt_type_mapped,
                "confidence": confidence,
            })
        
        logger.info(f"Loaded {len(edges)} edges from graph_canonical.db (confidence >= {min_confidence})")
        return edges

    def save_graph_relationships(self, db_manager, edges):
        """Insert graph DB edges into the pipeline relationships table."""
        count = 0
        for e in edges:
            try:
                db_manager.add_relationship(
                    src_id=None,
                    src_name=e["source_label"],
                    src_type=e["source_type"],
                    tgt_id=None,
                    tgt_name=e["target_label"],
                    tgt_type=e["target_type"],
                    relation=e["edge_type"],
                    source_data="EPSTEIN_COMMITTEE"
                )
                count += 1
            except Exception:
                pass
        logger.info(f"Inserted {count} graph DB edge relationships")
        return count

    def process_all(self, db_manager):
        """Full pipeline: CSV persons + graph DB edges."""
        total = 0
        
        # Phase 1: CSV
        persons = self.load_persons_csv()
        total += self.save_csv_relationships(db_manager, persons)
        
        # Phase 2: Graph DB edges
        edges = self.load_graph_db_edges(min_confidence=0.9)
        total += self.save_graph_relationships(db_manager, edges)
        
        logger.info(f"Epstein integration complete. Added {total} total relationships")
        return total
