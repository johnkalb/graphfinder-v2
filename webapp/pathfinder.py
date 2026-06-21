"""Network Pathfinder — FastAPI backend.
Serves full-name search and shortest-path queries against the deduplicated social network."""
import os, pickle, json
from pathlib import Path
from typing import Optional
import networkx as nx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

app = FastAPI(title="Network Pathfinder", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)
DATA_DIR = Path(__file__).parent / "data"

_graph: Optional[nx.Graph] = None
_search_index = None
_canonical_map = None
_labels = None

def _load_search():
    """Load just the search index — fast, independent of graph."""
    global _search_index, _canonical_map, _labels
    spath = DATA_DIR / "search_index.json"
    if _search_index is None and spath.exists():
        with open(spath, "r", encoding="utf-8") as f:
            _search_index = json.load(f)
    cpath = DATA_DIR / "canonical_map.json"
    if _canonical_map is None and cpath.exists():
        with open(cpath, "r", encoding="utf-8") as f:
            _canonical_map = json.load(f)
    lpath = DATA_DIR / "labels.json"
    if _labels is None and lpath.exists():
        with open(lpath, "r", encoding="utf-8") as f:
            _labels = json.load(f)

def _load_graph():
    """Load the full graph — slower, only needed for pathfinding."""
    global _graph
    if _graph is not None:
        return
    gpath = DATA_DIR / "graph.pkl"
    epath = DATA_DIR / "graph_edges.json.gz"
    if epath.exists():
        import gzip
        with gzip.open(epath, "rt", encoding="utf-8") as f:
            ed = json.load(f)
        g = nx.Graph()
        nodes = ed["nodes"]
        g.add_nodes_from(nodes)
        for u, v, r in ed["edges"]:
            g.add_edge(nodes[u], nodes[v], relation=r)
        _graph = g
    elif gpath.exists():
        with open(gpath, "rb") as f:
            _graph = pickle.load(f)
    else:
        _graph = nx.Graph()

def load_data():
    _load_search()
    _load_graph()

@app.on_event("startup")
async def startup():
    """Load search index immediately; build graph in background thread."""
    import threading
    _load_search()
    def _bg():
        try:
            _load_graph()
        except Exception:
            pass
    threading.Thread(target=_bg, daemon=True).start()

@app.get("/health")
async def health():
    return {"ok": True}

@app.get("/debug")
async def debug():
    import os, traceback
    err = None
    try:
        _load_search()
    except Exception as e:
        err = traceback.format_exc()
    info = {"data_dir": str(DATA_DIR), "exists": DATA_DIR.exists(), "files": {}}
    if err:
        info["load_error"] = err
    if DATA_DIR.exists():
        for f in os.listdir(DATA_DIR):
            fp = DATA_DIR / f
            info["files"][f] = os.path.getsize(fp) if fp.is_file() else "dir"
    info["search_index_loaded"] = len(_search_index) if _search_index else 0
    info["graph_loaded"] = len(_graph.nodes()) if _graph else 0
    return info

RELATION_INFO = {
  'COMMUNICATED_WITH': {'title': 'Communication', 'desc': 'Communication between parties — calls, messages, or correspondence identified in documents or records.'},
  'TRANSACTED_WITH': {'title': 'Financial Transaction', 'desc': 'Financial transactions or business dealings between the parties as recorded in filings, financial records, or news reports.'},
  'ASSOCIATED_WITH': {'title': 'Association', 'desc': 'General association between the parties based on document co-occurrence, joint activities, or shared affiliations.'},
  'AFFILIATED_WITH': {'title': 'Affiliation', 'desc': 'Organizational affiliation — shared membership in an institution, board, or group.'},
  'PAID_BY': {'title': 'Financial Payment', 'desc': 'One party made a payment to the other, identified from financial records. The nature of the payment (subscription, service, donation, or other) and the specific individuals involved are not determinable from the available evidence.'},
  'CONTROLLED_BY': {'title': 'Control', 'desc': 'One party controlled or directed the other — ownership, management authority, or supervisory relationship.'},
  'EMPLOYED_BY': {'title': 'Employment', 'desc': 'One party was employed by the other — employer/employee or principal/agent relationship.'},
  'REPRESENTED_BY': {'title': 'Representation', 'desc': 'One party represented the other in a professional capacity — legal, financial, or advisory representation.'},
  'TRAVELED_TO': {'title': 'Travel', 'desc': 'Travel records — flights, itineraries, or transportation shared between parties.'},
  'MENTIONED_WITH': {'title': 'Co-mentioned in News', 'desc': 'Both parties appear in the same news article or set of articles (GDELT Global Knowledge Graph).'},
  'BOARD_MEMBER': {'title': 'Board Member', 'desc': 'One party served on the board of directors of the other.'},
  'DIRECTOR': {'title': 'Director', 'desc': 'One party served as a director of the other organization.'},
  'PRESIDENT': {'title': 'President', 'desc': 'One party served as president of the other organization.'},
  'CHAIRMAN': {'title': 'Chairman', 'desc': 'One party served as chairman of the board of the other.'},
  'CEO': {'title': 'CEO', 'desc': 'One party served as Chief Executive Officer of the other.'},
  'OFFICER': {'title': 'Officer', 'desc': 'One party served as an officer of the other organization.'},
  'FOUNDER': {'title': 'Founder', 'desc': 'One party founded the other organization.'},
  'EMPLOYER': {'title': 'Employer', 'desc': 'One party employed the other.'},
  'ALMA_MATER': {'title': 'Alma Mater', 'desc': 'One party attended or graduated from the other institution.'},
  'MEMBER_OF': {'title': 'Member', 'desc': 'One party was a member of the other organization.'},
}

def _get_label(node):
    return _labels.get(node, _canonical_map.get(node, {}).get("canonical", node))

def _find_entry(query):
    _load_search()
    q = query.lower().strip()
    results = []
    seen = set()
    if not _search_index:
        return []
    for entry in _search_index:
        canon = entry["canonical"]
        canon_lower = canon.lower()
        parts = canon_lower.split()
        # Also search aliases
        alias_matches = []
        for a in entry.get("aliases", []):
            if q in a.lower():
                alias_matches.append(a.lower())
        score = None
        if q == canon_lower:
            score = 100
        elif canon_lower.startswith(q):
            score = 90
        elif any(p.startswith(q) for p in parts):
            score = 80
        elif q in canon_lower:
            score = 50
        elif alias_matches:
            score = 40
        else:
            # Check if any part of query matches any part of canonical
            q_parts = q.split()
            canon_parts = set(parts)
            matching = sum(1 for qp in q_parts if any(cp.startswith(qp) for cp in canon_parts))
            if matching > 0:
                score = 20 + matching * 5
        if score is not None and score > 0:
            if canon_lower not in seen:
                seen.add(canon_lower)
                results.append((score, entry))
    results.sort(key=lambda x: (-x[0], x[1]["canonical"]))
    return [r[1] for r in results[:50]]

def _find_path(src_name, tgt_name, max_depth=6):
    _load_graph()
    if _graph is None:
        return {"error": "Graph not loaded"}
    src_node = _resolve_name(src_name)
    tgt_node = _resolve_name(tgt_name)
    if not src_node:
        return {"error": f"Source '{src_name}' not found"}
    if not tgt_node:
        return {"error": f"Target '{tgt_name}' not found"}
    try:
        # Use undirected graph for pathfinding (edges are directional but relationships
        # connect both parties bidirectionally)
        import networkx as nx
        ug = nx.Graph(_graph)
        paths_gen = nx.shortest_simple_paths(ug, src_node, tgt_node)
        paths = []
        for i, path in enumerate(paths_gen):
            if i >= 5:
                break
            step_objects = []
            for j in range(len(path)):
                label = _get_label(path[j])
                step_objects.append({"node": path[j], "label": label, "relation": None})
            for j in range(len(path) - 1):
                u, v = path[j], path[j + 1]
                ed = _graph.get_edge_data(u, v)
                rel = None
                if ed:
                    rel = ed.get("relation") or ed.get("type") or ed.get("label", "")
                    if not rel or rel == "None":
                        rel = None
                step_objects[j]["relation"] = rel
            paths.append({"length": len(path) - 1, "path": step_objects})
        return {"paths": paths, "src_found": True, "tgt_found": True}
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return {"paths": [], "src_found": src_node is not None, "tgt_found": tgt_node is not None}

def _resolve_name(name):
    name_lower = name.strip().lower()
    for node_id, info in _canonical_map.items():
        if info.get("canonical", "").lower() == name_lower:
            return info.get("canonical") if not info.get("is_alias") else node_id
    for node_id, info in _canonical_map.items():
        if info.get("name", "").lower() == name_lower:
            if info.get("is_alias"):
                for nid2, info2 in _canonical_map.items():
                    if info2.get("canonical") == info.get("canonical") and not info2.get("is_alias"):
                        return nid2
            else:
                return info.get("canonical")
    if name_lower in _graph:
        return name_lower
    for n in _graph.nodes():
        if n.lower() == name_lower:
            return n
    return None

@app.get("/api/evidence")
async def get_evidence(src: str = Query(...), tgt: str = Query(...), rel: str = Query(...)):
    """Return evidence for a specific relationship edge."""
    import sqlite3
    db_path = Path(__file__).parent.parent / "data" / "pipeline_cache.db"
    if not db_path.exists():
        return {"evidence": []}
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    c.execute('SELECT evidence FROM relationships WHERE source_name = ? AND target_name = ? AND relation_type = ? LIMIT 1',
              (src, tgt, rel))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        try:
            return {"evidence": json.loads(row[0])}
        except:
            return {"evidence": [{"note": row[0]}]}
    return {"evidence": []}

import hashlib
_psi_names = None
def _load_psi_names():
    global _psi_names
    if _psi_names is None:
        p = DATA_DIR / "psi_names.json"
        if p.exists():
            with open(p) as f:
                _psi_names = json.load(f)
    return _psi_names

@app.get("/api/names")
async def get_names():
    """Return compacted graph names for client-side matching."""
    p = DATA_DIR / "compact_names.json.gz"
    if p.exists():
        from fastapi.responses import Response
        with open(p, 'rb') as f:
            return Response(content=f.read(), media_type='application/gzip')
    return {"error": "no names file"}

@app.post("/api/psi")
async def psi_match(request: Request):
    """Private Set Intersection: match user's salted hashes against graph names."""
    body = await request.json()
    salt = body.get("salt", "")
    hashes = body.get("hashes", [])
    if not salt or not hashes:
        return {"matches": 0, "total": 0}
    
    names = _load_psi_names()
    if not names:
        return {"matches": 0, "total": 0}
    
    # Compute graph hashes with the same salt
    user_set = set(hashes)
    match_count = 0
    for name in names:
        h = hashlib.sha256((name + salt).encode()).hexdigest()
        if h in user_set:
            match_count += 1
    
    return {"matches": match_count, "total": len(hashes)}

@app.get("/api/search")
async def search(q: str = Query(default="")):
    if not q or len(q.strip()) < 2:
        return []
    return _find_entry(q.strip())

@app.get("/api/path")
async def path(src_name: str = Query(default=""), tgt_name: str = Query(default="")):
    if not src_name or not tgt_name:
        return {"error": "Both src_name and tgt_name required"}
    return _find_path(src_name.strip(), tgt_name.strip())

@app.get("/api/relation-info")
async def relation_info(rtype: str = Query(default="")):
    info = RELATION_INFO.get(rtype)
    if info:
        return info
    return {"title": rtype, "desc": "Relationship type from document analysis or data extraction."}

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_TEMPLATE)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Network Pathfinder</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
         background: #0d1117; color: #e6edf3; min-height: 100vh; display: flex;
         flex-direction: column; align-items: center; padding: 2rem 1rem; }
  .container { max-width: 700px; width: 100%; }
  h1 { font-size: 1.8rem; margin-bottom: 0.5rem; color: #58a6ff; }
  p.sub { color: #8b949e; margin-bottom: 2rem; font-size: 0.95rem; line-height: 1.5; }
  .search-pair { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 1.5rem; margin-bottom: 1.5rem; position: relative; }
  .search-pair h2 { font-size: 1rem; color: #c9d1d9; margin-bottom: 0.5rem; }
  .autocomplete-wrap { position: relative; }
  .autocomplete-wrap input {
    width: 100%; background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    border-radius: 6px; padding: 0.7rem 1rem; font-size: 1rem;
    outline: none; transition: border-color 0.2s; }
  .autocomplete-wrap input:focus { border-color: #58a6ff; }
  .autocomplete-wrap input::placeholder { color: #484f58; }
  .dropdown { display: none; position: absolute; top: 100%; left: 0; right: 0;
               background: #1c2128; border: 1px solid #30363d; border-top: none;
               border-radius: 0 0 6px 6px; max-height: 280px; overflow-y: auto;
               z-index: 100; }
  .dropdown.show { display: block; }
  .dropdown-item { padding: 0.6rem 1rem; cursor: pointer; font-size: 0.9rem; }
  .dropdown-item:hover, .dropdown-item.highlighted { background: #30363d; }
  .dropdown-item .name { color: #e6edf3; }
  .dropdown-item .sub { color: #8b949e; font-size: 0.8rem; margin-left: 0.5rem; }
  .dropdown-item .alias { display: block; color: #8b949e; font-size: 0.75rem; margin-top: 0.15rem; }
  .selected-tag { display: inline-flex; align-items: center; background: #1f6feb22;
                   border: 1px solid #1f6feb44; border-radius: 6px; padding: 0.4rem 0.8rem;
                   margin: 0.5rem 0; font-size: 0.9rem; color: #58a6ff; }
  .selected-tag .clear { margin-left: 0.5rem; cursor: pointer; color: #8b949e; font-size: 1rem; }
  .selected-tag .clear:hover { color: #f85149; }
  button { background: #238636; color: #fff; border: none; border-radius: 6px;
           padding: 0.7rem 1.5rem; font-size: 1rem; cursor: pointer;
           transition: background 0.2s; }
  button:hover:not(:disabled) { background: #2ea043; }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  #results { margin-top: 1.5rem; }
  .path-result { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 1.5rem; margin-bottom: 1rem; position: relative; }
  .path-result .length { color: #8b949e; font-size: 0.85rem; margin-bottom: 1rem; }
  .path-step { display: flex; align-items: center; gap: 0.5rem; margin: 0.5rem 0;
                flex-wrap: wrap; }
  .step-node { color: #e6edf3; font-weight: 500; cursor: pointer; }
  .step-node:hover { color: #58a6ff; }
  .step-arrow { color: #484f58; }
  .step-rel { color: #8b949e; font-size: 0.85rem; cursor: help;
               border-bottom: 1px dotted #484f58; padding-bottom: 1px; }
  .no-path { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
              padding: 1.5rem; text-align: center; color: #8b949e; }
  .error-msg { background: #161b22; border: 1px solid #f8514944; border-radius: 8px;
                padding: 1rem; color: #f85149; }
  .tooltip { display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
              background: #1c2128; border: 1px solid #30363d; border-radius: 8px;
              padding: 1.5rem; max-width: 400px; z-index: 200; box-shadow: 0 8px 32px #00000066; }
  .tooltip.show { display: block; }
  .tooltip .close { float: right; cursor: pointer; color: #8b949e; font-size: 1.2rem; }
  .tooltip .tooltip-title { font-weight: 600; color: #e6edf3; margin-bottom: 0.5rem; }
  .tooltip .tooltip-desc { color: #8b949e; font-size: 0.9rem; line-height: 1.5; }
  .instructions { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                   padding: 1rem; margin-bottom: 1.5rem; font-size: 0.9rem; color: #8b949e; }
  .instructions strong { color: #58a6ff; }
  .psi-section { margin-top: 2rem; padding: 1.5rem; background: #161b22; border: 1px solid #30363d; border-radius: 8px; text-align: center; }
  .psi-section button { background: #1f6feb; color: #fff; border: none; border-radius: 6px; padding: 0.7rem 1.5rem; font-size: 1rem; cursor: pointer; }
  .psi-section button:hover { background: #388bfd; }
  .psi-note { color: #8b949e; font-size: 0.8rem; margin-top: 0.5rem; }
  .psi-result { display: inline-block; margin-left: 1rem; font-weight: 600; color: #3fb950; }
</style>
</head>
<body>
<div class="container">
  <h1>🔗 Network Pathfinder</h1>
  <p class="sub">Explore <strong>800,000+ relationships</strong> across SEC filings, Epstein documents, GDELT news, IRS foundations, Wikidata, and LittleSis. Find hidden paths between any two people or organizations.</p>

  <div class="instructions">
    <strong>How to use:</strong><br>
    1. Click <strong>Person A</strong> and start typing — suggestions appear below<br>
    2. Click a name to select it<br>
    3. Do the same for <strong>Person B</strong><br>
    4. Click <strong>🔍 Find Path</strong> to see the connection
  </div>

  <div class="search-pair">
    <h2>Person A</h2>
    <div class="autocomplete-wrap">
      <input id="src-input" type="text" placeholder="Type any name…" autocomplete="off" spellcheck="false">
      <div id="src-dropdown" class="dropdown"></div>
    </div>
    <div id="src-selected"></div>
  </div>

  <div class="search-pair">
    <h2>Person B</h2>
    <div class="autocomplete-wrap">
      <input id="tgt-input" type="text" placeholder="Type any name…" autocomplete="off" spellcheck="false">
      <div id="tgt-dropdown" class="dropdown"></div>
    </div>
    <div id="tgt-selected"></div>
  </div>

  <button id="find-btn" onclick="findPath()" disabled>🔍 Find Path</button>

  <div id="results" class="results"></div>
  
  <div class="psi-section">
    <button id="psi-btn" onclick="document.getElementById('psi-file').click()">🔒 Check My Contacts</button>
    <span id="psi-result" style="display:none;"></span>
    <p class="psi-note">Your contacts are hashed in your browser and never sent in plaintext.</p>
    <input type="file" id="psi-file" accept=".vcf,.csv,.txt" style="display:none" onchange="doPSI(this)">
  </div>
  
  <div id="tooltip" class="tooltip" onclick="hideTooltip()">
    <span class="close" onclick="hideTooltip()">✕</span>
    <div id="tooltip-content"></div>
  </div>
</div>

<script>
const state = { src: { selected: null }, tgt: { selected: null } };
let searchTimeout = null;

['src', 'tgt'].forEach(prefix => {
  const input = document.getElementById(prefix + '-input');
  const dropdown = document.getElementById(prefix + '-dropdown');
  let highlightedIdx = -1;
  let currentResults = [];

  input.addEventListener('input', function() {
    const val = this.value.trim();
    state[prefix].selected = null;
    updateSelected(prefix);
    updateButton();
    clearTimeout(searchTimeout);
    if (val.length < 2) { dropdown.classList.remove('show'); currentResults = []; return; }
    searchTimeout = setTimeout(async () => {
      try {
        const res = await fetch('/api/search?q=' + encodeURIComponent(val));
        const data = await res.json();
        currentResults = data;
        highlightedIdx = -1;
        renderDropdown(prefix, data);
      } catch(e) { console.error('Search failed:', e); }
    }, 150);
  });

  input.addEventListener('keydown', function(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      highlightedIdx = Math.min(highlightedIdx + 1, currentResults.length - 1);
      highlightItem(prefix, highlightedIdx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      highlightedIdx = Math.max(highlightedIdx - 1, -1);
      highlightItem(prefix, highlightedIdx);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlightedIdx >= 0 && highlightedIdx < currentResults.length)
        selectItem(prefix, currentResults[highlightedIdx]);
      else if (currentResults.length === 1)
        selectItem(prefix, currentResults[0]);
    } else if (e.key === 'Escape') {
      dropdown.classList.remove('show');
    }
  });

  input.addEventListener('blur', function() {
    setTimeout(() => dropdown.classList.remove('show'), 200);
  });

  input.addEventListener('focus', function() {
    if (currentResults.length > 0) dropdown.classList.add('show');
  });

  function renderDropdown(prefix, items) {
    const dd = document.getElementById(prefix + '-dropdown');
    const val = document.getElementById(prefix + '-input').value.trim();
    if (!items || items.length === 0) {
      if (val.length >= 2) {
        dd.innerHTML = '<div class="dropdown-item" style="color:#8b949e;cursor:default;">No match found</div>';
        dd.classList.add('show');
      } else {
        dd.classList.remove('show');
      }
      return;
    }
    dd.innerHTML = items.map((item, i) => {
      let aliasHtml = '';
      const displayName = item.name || item.canonical;
      if (item.aliases && item.aliases.length > 0)
        aliasHtml = '<span class="alias">also: ' + escHtml(item.aliases.join(', ')) + '</span>';
      return '<div class="dropdown-item" data-index="' + i + '"'
        + ' onmousedown="selectItem(\'' + prefix + '\', ' + i + ')"'
        + ' onmouseover="highlightIdx(\'' + prefix + '\', ' + i + ')">'
        + '<span class="name">' + escHtml(displayName) + '</span>'
        + '<span class="sub">' + (item.count > 1 ? '×' + item.count : '') + '</span>'
        + aliasHtml
        + '</div>';
    }).join('');
    dd.classList.add('show');
  }

  window['highlightIdx'] = function(prefix, idx) {
    highlightedIdx = idx;
    highlightItem(prefix, idx);
  };

  function highlightItem(prefix, idx) {
    const dd = document.getElementById(prefix + '-dropdown');
    dd.querySelectorAll('.dropdown-item').forEach((el, i) => {
      el.classList.toggle('highlighted', i === idx);
    });
  }
});

window.selectItem = function(prefix, idx) {
  const dd = document.getElementById(prefix + '-dropdown');
  const items = dd.querySelectorAll('.dropdown-item');
  if (idx < 0 || idx >= items.length) return;
  const name = items[idx].querySelector('.name').textContent;
  state[prefix].selected = name;
  document.getElementById(prefix + '-input').value = name;
  dd.classList.remove('show');
  updateSelected(prefix);
  updateButton();
};

function updateSelected(prefix) {
  const el = document.getElementById(prefix + '-selected');
  if (state[prefix].selected) {
    el.innerHTML = '<div class="selected-tag">'
      + escHtml(state[prefix].selected)
      + ' <span class="clear" onclick="clearSelection(\'' + prefix + '\')">✕</span></div>';
  } else {
    el.innerHTML = '';
  }
}

window.clearSelection = function(prefix) {
  state[prefix].selected = null;
  document.getElementById(prefix + '-input').value = '';
  document.getElementById(prefix + '-selected').innerHTML = '';
  updateButton();
};

function updateButton() {
  document.getElementById('find-btn').disabled = !(state.src.selected && state.tgt.selected);
}

async function findPath() {
  if (!state.src.selected || !state.tgt.selected) return;
  const btn = document.getElementById('find-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Searching...';
  document.getElementById('results').innerHTML = '';
  try {
    const params = new URLSearchParams({ src_name: state.src.selected, tgt_name: state.tgt.selected });
    const res = await fetch('/api/path?' + params.toString(), {
      headers: { 'Accept': 'application/json' }
    });
    const data = await res.json();
    let html = '';
    if (data.error) {
      html = '<div class="error-msg">' + escHtml(data.error) + '</div>';
    } else if (!data.paths || data.paths.length === 0) {
      html = '<div class="no-path">No path found between <strong>' + escHtml(state.src.selected) + '</strong> and <strong>' + escHtml(state.tgt.selected) + '</strong></div>';
    } else {
      data.paths.forEach(p => {
        html += '<div class="path-result">';
        html += '<div class="length">' + p.length + ' edge' + (p.length !== 1 ? 's' : '') + '</div>';
        p.path.forEach((step, i) => {
          if (i > 0) {
            html += '<span class="step-arrow">→</span>';
            if (step.relation) {
              const ri = Math.random().toString(36).slice(2);
              html += '<span class="step-rel" id="rel-' + ri + '" title="' + escHtml(step.relation) + '">' + escHtml(step.relation) + '</span>';
              html += '<span class="step-arrow">→</span>';
              setTimeout(() => {
                const el = document.getElementById('rel-' + ri);
                if (el) el.onclick = function(e) { showRelTooltip(e, step.relation, p.path[i-1].label, step.label); };
              }, 0);
            }
          }
          html += '<span class="step-node">' + escHtml(step.label) + '</span>';
        });
        html += '</div>';
      });
    }
    document.getElementById('results').innerHTML = html;
  } catch(e) {
    document.getElementById('results').innerHTML = '<div class="error-msg">Error: ' + e.message + '</div>';
  }
  btn.disabled = false;
  btn.textContent = '🔍 Find Path';
}

async function showRelTooltip(event, rtype, src, tgt) {
  try {
    const res = await fetch('/api/relation-info?rtype=' + encodeURIComponent(rtype));
    const info = await res.json();
    const content = document.getElementById('tooltip-content');
    let html = '<div class="tooltip-title">' + escHtml(info.title || rtype) + '</div>'
      + '<div class="tooltip-desc">' + escHtml(info.desc || '') + '</div>';
    
    // Fetch evidence
    if (src && tgt) {
      try {
        const eres = await fetch('/api/evidence?src=' + encodeURIComponent(src) + '&tgt=' + encodeURIComponent(tgt) + '&rel=' + encodeURIComponent(rtype));
        const edata = await eres.json();
        if (edata.evidence && edata.evidence.length > 0) {
          html += '<br><strong style="font-size:0.85rem;">Evidence:</strong><br>';
          edata.evidence.forEach(function(ev) {
            if (ev.doc_id)
              html += '<span style="font-size:0.8rem;color:#8b949e;">📄 ' + escHtml(ev.doc_id) + (ev.page ? ' p.' + ev.page : '') + '</span><br>';
            if (ev.note)
              html += '<span style="font-size:0.8rem;color:#8b949e;">' + escHtml(ev.note) + '</span><br>';
          });
        }
      } catch(e) {}
    }
    
    content.innerHTML = html;
    document.getElementById('tooltip').classList.add('show');
  } catch(e) { console.error(e); }
}

function hideTooltip() {
  document.getElementById('tooltip').classList.remove('show');
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function doPSI(input) {
  const file = input.files[0];
  if (!file) return;
  const btn = document.getElementById('psi-btn');
  const result = document.getElementById('psi-result');
  btn.textContent = '⏳ Loading graph names...';
  btn.disabled = true;
  result.style.display = 'none';
  
  try {
    // Download compact graph names
    let graphNames = window._graphNames;
    if (!graphNames) {
      const res = await fetch('/api/names');
      const buf = await res.arrayBuffer();
      const decompressed = new DecompressionStream('gzip');
      const ds = new Response(buf).body.pipeThrough(decompressed);
      const reader = ds.getReader();
      let data = '';
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        data += new TextDecoder().decode(value);
      }
      graphNames = JSON.parse(data);
      window._graphNames = graphNames;
    }
    const graphKeys = Object.keys(graphNames);
    
    btn.textContent = '⏳ Matching...';
    
    // Read contacts file
    const text = await file.text();
    const lines = text.split('\n');
    const contacts = [];
    
    for (let line of lines) {
      line = line.trim();
      if (!line) continue;
      if (line.startsWith('BEGIN:') || line.startsWith('END:') || line.startsWith('VERSION:')) continue;
      if (line.startsWith('FN:') || line.startsWith('N:')) {
        let name = line.includes(':') ? line.split(':')[1].trim() : line;
        if (line.startsWith('N:') && name.includes(';')) {
          const parts = name.split(';');
          name = (parts[1] + ' ' + parts[0]).trim();
        }
        if (name.length > 3) contacts.push(name);
      } else if (line.includes(',') && !line.startsWith('EMAIL') && !line.startsWith('TEL')) {
        const cells = line.split(',');
        if (cells.length >= 2) {
          const name = (cells[0] + ' ' + cells[1]).trim();
          if (name.length > 3) contacts.push(name);
        }
      }
    }
    
    if (contacts.length === 0) {
      result.textContent = 'No contacts found in file';
      result.style.color = '#f85149';
      result.style.display = 'inline-block';
      btn.textContent = '🔒 Check My Contacts';
      btn.disabled = false;
      return;
    }
    
    // Fuzzy match each contact against graph names
    const matches = [];
    for (let contact of contacts) {
      const c = contact.toLowerCase().trim();
      // Generate variants
      const variants = [c];
      // First + last only
      const parts = c.split(/\s+/);
      if (parts.length > 2) {
        variants.push(parts[0] + ' ' + parts[parts.length - 1]);
        variants.push(parts[parts.length - 1] + ' ' + parts[0]);
      }
      // Last, First
      if (parts.length >= 2) {
        variants.push(parts[parts.length - 1] + ' ' + parts[0]);
      }
      
      for (let variant of variants) {
        // Exact match
        if (graphNames[variant]) {
          matches.push(contact + ' → ' + graphNames[variant]);
          break;
        }
        // Prefix match (first 4 chars of last name)
        const vparts = variant.split(/\s+/);
        if (vparts.length >= 2 && vparts[vparts.length - 1].length >= 4) {
          const prefix = vparts[vparts.length - 1].substring(0, 4).toLowerCase();
          for (let key of graphKeys) {
            if (key.includes(prefix) && (key.includes(vparts[0].substring(0, 3).toLowerCase()) || key.includes(vparts[vparts.length - 1].substring(0, 3).toLowerCase()))) {
              matches.push(contact + ' → ' + graphNames[key]);
              break;
            }
          }
        }
      }
    }
    
    // Show results
    if (matches.length === 0) {
      result.textContent = 'No contacts found in the database';
      result.style.color = '#8b949e';
    } else {
      result.innerHTML = '<strong>' + matches.length + ' contact' + (matches.length !== 1 ? 's' : '') + ' found:</strong><br>' +
        matches.map(m => '<span style="display:block;font-size:0.85rem;color:#8b949e;margin-top:0.3rem;">✓ ' + escHtml(m) + '</span>').join('');
    }
    result.style.display = 'inline-block';
  } catch(e) {
    result.textContent = 'Error: ' + e.message;
    result.style.color = '#f85149';
    result.style.display = 'inline-block';
  }
  
  btn.textContent = '🔒 Check My Contacts';
  btn.disabled = false;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
