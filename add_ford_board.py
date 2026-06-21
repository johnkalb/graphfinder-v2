"""Add Ford/MacArthur board members as foundation connections and check paths to graph."""
import sys, os
sys.path.insert(0, r"C:\Users\johnk")
from src.config import DB_PATH
from src.data.db_manager import DBManager
from src.graph.graph_compiler import GraphCompiler

db = DBManager(DB_PATH)

# Ford Foundation board
ford_board = [
    ("ERIC W DOPPSTADT", "VP & CHIEF INVESTMENT OFFICER"),
    ("DARREN WALKER", "PRESIDENT & TRUSTEE"),
    ("URSULA M BURNS", "TRUSTEE"),
    ("HENRY FORD III", "TRUSTEE"),
    ("THOMAS L KEMPNER JR", "TRUSTEE"),
    ("PAULA MORENO", "TRUSTEE"),
    ("LAURENE POWELL JOBS", "TRUSTEE"),
    ("CHUCK ROBBINS", "TRUSTEE"),
    ("BRYAN STEVENSON", "TRUSTEE"),
    ("GEORGE WALKER", "TRUSTEE"),
    ("FRANCISCO G CIGARROA", "TRUSTEE & CHAIR"),
    ("CATALINA DEVANDAS", "TRUSTEE"),
    ("AMY C FALLS", "TRUSTEE"),
    ("LOURDES LOPEZ", "TRUSTEE"),
    ("GBENGA OYEBODE", "TRUSTEE"),
    ("AI-JEN POO", "TRUSTEE"),
    ("GABRIELLE SULZBERGER", "TRUSTEE"),
]

# MacArthur Foundation board
macarthur_board = [
    ("MARTHA L MINOW", "BOARD CHAIR"),
    ("STEPHANIE K BELL-ROSE", "BOARD TRUSTEE"),
    ("JULIE T KATZMAN", "BOARD TRUSTEE"),
    ("PAUL KLINGENSTEIN", "BOARD TRUSTEE"),
    ("JAMES MANYIKA", "BOARD TRUSTEE"),
    ("SENDHIL MULLAINATHAN", "BOARD TRUSTEE"),
    ("CECILIA MUNOZ", "BOARD TRUSTEE"),
    ("ALONDRA NELSON", "BOARD TRUSTEE"),
    ("OLUFUNMILAYO I OLOPADE", "BOARD TRUSTEE"),
    ("JUAN SALGADO", "BOARD TRUSTEE"),
    ("RUTH J SIMMONS", "BOARD TRUSTEE"),
]

added = 0
for name, title in ford_board:
    try:
        db.add_relationship(None, name, "PERSON", None, "Ford Foundation", "NONPROFIT", "BOARD_MEMBER", "IRS_990")
        added += 1
    except: pass

for name, title in macarthur_board:
    try:
        db.add_relationship(None, name, "PERSON", None, "MacArthur Foundation", "NONPROFIT", "BOARD_MEMBER", "IRS_990")
        added += 1
    except: pass

print(f"Added {added} board member relationships")

# Now compile graph and check paths
compiler = GraphCompiler(db)
g = compiler.build_graph()
print(f"Graph: {len(g)} nodes, {len(g.edges())} edges")

# Find Ursula Burns specifically
for node in g.nodes():
    if 'ursula' in node.lower() and 'burns' in node.lower():
        print(f"\n'{node}' connections ({len(list(g.neighbors(node)))}):")
        for n in list(g.neighbors(node))[:5]:
            print(f"  → {n}")

# Check which board members connect to the existing graph
print("\n\nCross-connections from foundation boards to main graph:")
all_trustees = [n for n, _ in ford_board] + [n for n, _ in macarthur_board]
connected = 0
for name in all_trustees:
    for node in g.nodes():
        if name.lower().strip() in node.lower() or node.lower() in name.lower().strip():
            neighbors = [n for n in g.neighbors(node) if n not in ("Ford Foundation", "MacArthur Foundation")]
            if neighbors:
                connected += 1
                print(f"  ✅ {g.nodes[node].get('label', node)} → {neighbors[:3]}")
                break
            else:
                print(f"  ⚠️ {g.nodes[node].get('label', node)} → (only connected to foundation)")
                break

print(f"\n{connected}/{len(all_trustees)} board members connect beyond the foundation")
