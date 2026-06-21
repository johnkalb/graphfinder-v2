import os
import networkx as nx
import csv
from src.utils.logger import setup_custom_logger

logger = setup_custom_logger("gephi_exporter")

class GephiExporter:
    def __init__(self):
        pass

    def export_all(self, g, gexf_path="data/output/graph.gexf", nodes_csv="data/output/nodes.csv", edges_csv="data/output/edges.csv"):
        """
        Export a NetworkX graph to GEXF and CSV formats optimized for Gephi.
        Creates parent directories if they don't exist.
        """
        logger.info("Starting Gephi export process...")
        
        # Ensure target directories exist
        for path in [gexf_path, nodes_csv, edges_csv]:
            if path:
                dir_name = os.path.dirname(path)
                if dir_name and not os.path.exists(dir_name):
                    logger.debug(f"Creating directory: {dir_name}")
                    os.makedirs(dir_name, exist_ok=True)

        # 1. Export GEXF (Best format for Gephi as it retains node & edge properties)
        # Gephi requires attributes to be strings or numbers.
        # Create a copy or clean copy to ensure GEXF compatibility by casting attributes to supported types.
        g_clean = nx.DiGraph()
        
        for node, attrs in g.nodes(data=True):
            clean_attrs = {}
            for k, val in attrs.items():
                if val is None:
                    clean_attrs[k] = ""
                elif isinstance(val, (bool, int, float, str)):
                    clean_attrs[k] = val
                else:
                    clean_attrs[k] = str(val)
            g_clean.add_node(node, **clean_attrs)
            
        for u, v, attrs in g.edges(data=True):
            clean_attrs = {}
            for k, val in attrs.items():
                if val is None:
                    clean_attrs[k] = ""
                elif isinstance(val, (bool, int, float, str)):
                    clean_attrs[k] = val
                else:
                    clean_attrs[k] = str(val)
            g_clean.add_edge(u, v, **clean_attrs)

        if gexf_path:
            logger.info(f"Writing GEXF file to {gexf_path}...")
            nx.write_gexf(g_clean, gexf_path)

        # 2. Export Node CSV (Id, Label, Type)
        if nodes_csv:
            logger.info(f"Writing Node CSV to {nodes_csv}...")
            with open(nodes_csv, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(["Id", "Label", "Type"])
                for node, attrs in g.nodes(data=True):
                    # Using node as Id and Label (or attrs.get("label", node))
                    label = attrs.get("label", node)
                    node_type = attrs.get("type", "PERSON")
                    writer.writerow([node, label, node_type])

        # 3. Export Edge CSV (Source, Target, Type, Relation, DatasetSource)
        if edges_csv:
            logger.info(f"Writing Edge CSV to {edges_csv}...")
            with open(edges_csv, mode='w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                # Gephi expects "Source" and "Target". "Type" is edge type (Directed/Undirected)
                writer.writerow(["Source", "Target", "Type", "Relation", "DatasetSource"])
                for u, v, attrs in g.edges(data=True):
                    relation = attrs.get("relation", "ASSOCIATE")
                    source_data = attrs.get("source", "SEC")
                    writer.writerow([u, v, "Directed", relation, source_data])
                
        logger.info("Gephi export completed successfully.")
