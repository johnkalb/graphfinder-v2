import networkx as nx
import re
import sqlite3
from src.utils.logger import setup_custom_logger

logger = setup_custom_logger("graph_compiler")

class GraphCompiler:
    def __init__(self, db_manager):
        self.db = db_manager

    def clean_name(self, name):
        """
        Robustly clean entity names to prevent duplicate nodes in NetworkX.
        Strips leading/trailing whitespace, commas, and trailing corporate suffixes 
        like Inc, Corp, LLC, Co, Ltd, etc. with robust handling of internal dots and casing.
        """
        if not name:
            return ""
        
        name = name.strip()
        
        # Regex to match common corporate suffixes at the end of a string,
        # handling optional trailing period, optional internal dots, and case insensitivity.
        # Examples matched: "Inc.", "Inc", "Corp.", "Corp", "Corporation", "Ltd.", "Ltd", "LLC", "L.L.C.", "Co", "Co.", "Company", "L.P.", "LP"
        suffix_pattern = re.compile(
            r'[\s,]+(?:inc|incorporated|corp|corporation|ltd|limited|l\.?l\.?c\.?|co|company|l\.?p\.?|p\.?l\.?c\.?|s\.?a\.?|a\.?g\.?)\.?$', 
            re.IGNORECASE
        )
        
        # Clean repeatedly in case of nested suffixes (e.g., "Apple Co., Ltd.")
        prev_name = None
        while name != prev_name:
            prev_name = name
            # Strip trailing punctuation except dots to expose the suffix clearly
            name = name.strip().rstrip(',;:').strip()
            # Substitute matching corporate suffix
            name = suffix_pattern.sub('', name)
            
        # Final clean of trailing/leading punctuation except dots and whitespace
        name = name.strip().rstrip(',;:').strip()
        
        # Normalize internal whitespace
        name = re.sub(r'\s+', ' ', name)
        
        return name

    def build_graph(self):
        """
        Queries the relationships SQLite database, normalizes node names, 
        resolves duplicate entities, and compiles a NetworkX DiGraph.
        """
        g = nx.DiGraph()
        
        logger.info("Starting graph compilation from DB relationships...")
        
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data 
            FROM relationships
        """)
        rows = cursor.fetchall()
        conn.close()
        
        logger.debug(f"Retrieved {len(rows)} raw relationship rows from database.")
        
        for src_id, src_name, src_type, tgt_id, tgt_name, tgt_type, relation, src_data in rows:
            src_clean = self.clean_name(src_name)
            tgt_clean = self.clean_name(tgt_name)
            
            if not src_clean or not tgt_clean:
                continue
                
            # Add or update Source Node
            if not g.has_node(src_clean):
                g.add_node(src_clean, type=src_type, id=src_id if src_id else "")
            else:
                # If existing node doesn't have an ID but this record does, enrich it
                if src_id and not g.nodes[src_clean].get("id"):
                    g.nodes[src_clean]["id"] = src_id
                # Ensure type is set if not already
                if src_type and not g.nodes[src_clean].get("type"):
                    g.nodes[src_clean]["type"] = src_type
                    
            # Add or update Target Node
            if not g.has_node(tgt_clean):
                g.add_node(tgt_clean, type=tgt_type, id=tgt_id if tgt_id else "")
            else:
                # If existing node doesn't have an ID but this record does, enrich it
                if tgt_id and not g.nodes[tgt_clean].get("id"):
                    g.nodes[tgt_clean]["id"] = tgt_id
                # Ensure type is set if not already
                if tgt_type and not g.nodes[tgt_clean].get("type"):
                    g.nodes[tgt_clean]["type"] = tgt_type
            
            # Add/overwrite Directed Edge with relationship details
            # Gephi and other tools can consume edge attributes
            g.add_edge(
                src_clean, 
                tgt_clean, 
                relation=relation, 
                source=src_data
            )
            
        logger.info(f"Graph compilation complete. Compiled {g.number_of_nodes()} nodes and {g.number_of_edges()} edges.")
        return g
