"""Network Pathfinder — FastAPI backend.
Serves full-name search and shortest-path queries against the deduplicated social network."""
import os, pickle, json, sqlite3, hashlib, re
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone
import networkx as nx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field, ConfigDict
import base64
import pysodium
try:
    from contact_psi import app as contact_psi_app
    from contact_psi import oprf as contact_psi_oprf
    from contact_psi import manifest as contact_psi_manifest
except ImportError:
    from webapp.contact_psi import app as contact_psi_app
    from webapp.contact_psi import oprf as contact_psi_oprf
    from webapp.contact_psi import manifest as contact_psi_manifest
try:
    from tester_operations import send_email
except ImportError:
    from webapp.tester_operations import send_email

app = FastAPI(title="Network Pathfinder", version="2.0.0", docs_url=None, redoc_url=None, openapi_url=None)
DATA_DIR = Path(__file__).parent / "data"

@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time, uuid
    start_time = time.time()
    
    session_id = request.cookies.get("session_id")
    is_new_session = False
    if not session_id:
        session_id = str(uuid.uuid4())
        is_new_session = True
        
    request.state.session_id = session_id
    response = await call_next(request)
    
    if is_new_session:
        response.set_cookie(
            key="session_id",
            value=session_id,
            httponly=True,
            samesite="lax",
            max_age=3600 * 24 * 30  # 30 days
        )
        
    process_time = (time.time() - start_time) * 1000
    _log_performance_metrics(request.url.path, process_time, response.status_code)
    
    # Log anonymous user behavior event (excluding health/debug/metrics paths)
    path = request.url.path
    if path not in ["/health", "/metrics", "/meminfo", "/debug", "/loadgraph"]:
        _log_anonymous_event(session_id, "request", {
            "method": request.method,
            "path": path,
            "query_params": dict(request.query_params)
        })
        
    return response

try:
    from link_scoring import (CATEGORY_PROB as _CATEGORY_PROB, CATEGORY_DESC as _CATEGORY_DESC,
                              METHODOLOGY as _METHODOLOGY, path_probability as _path_probability,
                              path_probability_guided as _path_probability_guided,
                              FORWARD_PROB as _FORWARD_PROB)
except ImportError:
    from webapp.link_scoring import (CATEGORY_PROB as _CATEGORY_PROB, CATEGORY_DESC as _CATEGORY_DESC,
                                     METHODOLOGY as _METHODOLOGY, path_probability as _path_probability,
                                     path_probability_guided as _path_probability_guided,
                                     FORWARD_PROB as _FORWARD_PROB)

try:
    from database import init_db as init_submission_db, init_ops_db, init_legal_compliance_db, DB_PATH as SUBMISSIONS_DB_PATH
    from test_department import (
        add_feedback as td_add_feedback,
        add_note as td_add_note,
        get_tester_snapshot as td_get_tester_snapshot,
        list_attention_queue as td_list_attention_queue,
        record_invitation as td_record_invitation,
        record_usage_event as td_record_usage_event,
    )
except ImportError:
    from webapp.database import init_db as init_submission_db, init_ops_db, init_legal_compliance_db, DB_PATH as SUBMISSIONS_DB_PATH
    from webapp.test_department import (
        add_feedback as td_add_feedback,
        add_note as td_add_note,
        get_tester_snapshot as td_get_tester_snapshot,
        list_attention_queue as td_list_attention_queue,
        record_invitation as td_record_invitation,
        record_usage_event as td_record_usage_event,
    )

_graph: Optional[nx.Graph] = None
_search_index = None
_canonical_map = None
_labels = None
_deceased = None  # {lowercase name: death date} -- Wikidata P570, authoritative

def _load_deceased():
    """Load authoritative date-of-death info (Wikidata P570) for known people."""
    global _deceased
    if _deceased is not None:
        return _deceased
    _deceased = {}
    dpath = DATA_DIR / "deceased.json"
    if dpath.exists():
        try:
            with open(dpath, "r", encoding="utf-8") as f:
                _deceased = {k.lower(): v for k, v in json.load(f).items()}
        except Exception:
            _deceased = {}
    return _deceased


def _load_search():
    """Load just the search index — fast, independent of graph."""
    global _search_index, _canonical_map, _labels
    spath = DATA_DIR / "search_index.json"
    if _search_index is None and spath.exists():
        with open(spath, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        # Drop known unresolved-ID placeholder entries once, here, rather
        # than checking on every _find_entry() call: with ~1.8M entries,
        # an extra per-entry regex check on every search request roughly
        # doubled an already too-slow linear scan (measured: 3.92s -> 7.53s
        # for a single query) -- the scan itself being >2s is a pre-existing
        # issue from the index's daily growth, unrelated to this filter,
        # but there's no reason to also pay the filtering cost on every
        # request when it only needs to happen once per process lifetime.
        _search_index = [e for e in loaded if not _is_placeholder_entity(e.get("canonical", ""))]
    cpath = DATA_DIR / "canonical_map.json"
    if _canonical_map is None and cpath.exists():
        with open(cpath, "r", encoding="utf-8") as f:
            _canonical_map = json.load(f)
    lpath = DATA_DIR / "labels.json"
    if _labels is None and lpath.exists():
        with open(lpath, "r", encoding="utf-8") as f:
            _labels = json.load(f)

def _load_graph():
    """Load the scored NetworkX graph. Each edge carries:
       prob  (P would take a call), weight (-log prob, for shortest path),
       cats  (contributing relationship categories)."""
    global _graph
    if _graph is not None:
        return
    import gzip, math
    spath = DATA_DIR / "graph_scored.json.gz"
    epath = DATA_DIR / "graph_edges.json.gz"
    if spath.exists():
        with gzip.open(spath, "rt", encoding="utf-8") as f:
            ed = json.load(f)
        nodes = ed["nodes"]
        edge_list = ed["edges"]
        ed = None
        g = nx.Graph()
        g.add_nodes_from(nodes)
        n_edges = len(edge_list)
        # Edge weight = -log(prob) + forwarding penalty -log(FORWARD_PROB).
        # This makes shortest-path (Dijkstra / shortest_simple_paths) rank by the
        # TRUE combined probability product(edge probs) * FORWARD_PROB^(hops-1):
        # the per-edge forwarding penalty differs from the (hops-1) form by a single
        # constant identical for every path, so ranking is exact while shorter paths
        # are correctly favored over long ones.
        _fwd_penalty = -math.log(_FORWARD_PROB) if _FORWARD_PROB > 0 else 0.0
        for idx in range(n_edges):
            u, v, prob, cats = edge_list[idx]
            p = prob if prob > 1e-9 else 1e-9
            g.add_edge(nodes[u], nodes[v], prob=prob,
                       weight=-math.log(p) + _fwd_penalty, cats=cats)
            edge_list[idx] = None
        edge_list = None
        _graph = g
    elif epath.exists():
        # Fallback: legacy unscored edge list
        with gzip.open(epath, "rt", encoding="utf-8") as f:
            ed = json.load(f)
        nodes = ed["nodes"]
        edge_list = ed["edges"]
        ed = None
        g = nx.Graph()
        g.add_nodes_from(nodes)
        for idx in range(len(edge_list)):
            u, v, r = edge_list[idx]
            g.add_edge(nodes[u], nodes[v], relation=r, prob=0.5, weight=0.693, cats=[])
            edge_list[idx] = None
        edge_list = None
        _graph = g
    else:
        _graph = nx.Graph()

def load_data():
    _load_search()
    _load_graph()

@app.on_event("startup")
async def startup():
    """Load only the search index at startup; graph loads lazily on first path request."""
    init_submission_db(str(DATA_DIR / "test_department.db"))
    init_ops_db(str(DATA_DIR / "ops_metrics.db"))
    init_legal_compliance_db(str(DATA_DIR / "legal_compliance.db"))
    _load_search()
    # Configure the encrypted, prefix-sharded manifest only when production
    # secrets and the generated shard directory are present. Never log or
    # return the server secret. The server never loads shard contents into
    # memory -- lookups happen entirely client-side after it fetches only
    # the shard(s) it needs; the server's role is limited to the blind OPRF
    # evaluation and serving static shard files.
    secret_b64 = os.environ.get("CONTACT_PSI_SERVER_SECRET")
    manifest_dir = DATA_DIR / "contact_psi_manifest"
    if secret_b64 and manifest_dir.is_dir():
        try:
            secret = base64.b64decode(secret_b64, validate=True)
            version = int(os.environ.get("CONTACT_PSI_KEY_VERSION", "1"))
            shard_files = sorted(manifest_dir.glob("contact_psi_manifest_*.json.gz"))
            if not shard_files:
                raise ValueError("no manifest shards found")
            # Cache-busting signal only (client uses this to decide whether
            # its cached shard downloads are stale) -- name + size + mtime
            # is enough to detect a rebuilt manifest without reading and
            # hashing the full ~650MB of shard content on every process
            # startup, which measurably slowed cold start (confirmed: ~9s
            # added per boot) and risks tripping health-check timeouts.
            digest = hashlib.sha256()
            for shard_path in shard_files:
                stat = shard_path.stat()
                digest.update(f"{shard_path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
            contact_psi_app.configure(secret, version, None, {
                "key_version": version,
                "sharded": True,
                "shard_hex_chars": contact_psi_manifest.SHARD_HEX_CHARS,
                "manifest_shard_url_template": "/api/contacts/manifest-shard/{shard}",
                "num_shards": len(shard_files),
                "manifest_digest": digest.hexdigest(),
                "built_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception:
            # An unavailable/malformed optional artifact should not prevent
            # the graph search application from starting.
            pass

@app.get("/meminfo")
async def meminfo():
    import os
    info = {}
    try:
        # Read cgroup memory limit (what the container actually has)
        for path in ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
            if os.path.exists(path):
                with open(path) as f:
                    info["cgroup_limit"] = f.read().strip()
                break
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal") or line.startswith("MemAvailable"):
                    info[line.split(":")[0]] = line.split(":")[1].strip()
    except Exception as e:
        info["error"] = str(e)
    return info

@app.get("/loadgraph")
async def loadgraph():
    """Manually trigger graph load with memory tracking."""
    import os
    steps = []
    def mem():
        try:
            with open(f"/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS"):
                        return line.split(":")[1].strip()
        except: return "?"
    steps.append(f"start: {mem()}")
    try:
        _load_graph()
        steps.append(f"after load: {mem()}")
        steps.append(f"nodes: {len(_graph.nodes())}, edges: {len(_graph.edges())}")
    except Exception as e:
        import traceback
        steps.append(f"ERROR: {traceback.format_exc()[:500]}")
    return {"steps": steps}

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
    info["graph_loaded"] = len(_graph.nodes()) if (_graph is not None and hasattr(_graph, "nodes")) else 0
    return info


def _test_department_db_path() -> str:
    return str(DATA_DIR / "test_department.db")


def _request_user_email(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None
    email = request.headers.get("Cf-Access-Authenticated-User-Email")
    if email:
        return email.strip().lower()
    return None


def _log_tester_usage(request: Optional[Request], event_type: str, metadata: Optional[dict] = None):
    email = _request_user_email(request)
    if not email:
        return None
    return td_record_usage_event(
        _test_department_db_path(),
        tester_email=email,
        event_type=event_type,
        metadata=metadata,
    )

def _log_performance_metrics(route: str, latency_ms: float, status_code: int):
    db_path = DATA_DIR / "ops_metrics.db"
    if not db_path.exists():
        init_ops_db(str(db_path))
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO request_logs (route, latency_ms, status_code) VALUES (?, ?, ?)",
                 (route, latency_ms, status_code))
    conn.commit()
    conn.close()

def _log_anonymous_event(session_id: str, event_type: str, metadata: dict):
    db_path = DATA_DIR / "ops_metrics.db"
    if not db_path.exists():
        init_ops_db(str(db_path))
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO anonymous_events (session_id, event_type, metadata) VALUES (?, ?, ?)",
            (session_id, event_type, json.dumps(metadata))
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

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
  'POSITION': {'title': 'Position Held', 'desc': 'One party held a position (role, title, or office) at the other organization, per LittleSis records.'},
  'DONATION': {'title': 'Donation', 'desc': 'One party made a political or charitable donation involving the other, per LittleSis campaign-finance records.'},
  'MEMBERSHIP': {'title': 'Membership', 'desc': 'One party was a member of the other organization or group, per LittleSis records.'},
  'CO_DIRECTOR': {'title': 'Co-Director', 'desc': 'Both parties served as directors of the same company, per SEC filings.'},
  'CO_OFFICER': {'title': 'Co-Officer', 'desc': 'Both parties served as officers of the same company, per SEC filings.'},
  'ALUMNI_OF': {'title': 'Alumnus', 'desc': 'One party attended or graduated from the other institution.'},
  'BOARD_MEMBER_OF': {'title': 'Board Member', 'desc': 'One party served on the board of the other organization.'},
  'VISITING_PROFESSOR': {'title': 'Visiting Professor', 'desc': 'One party served as a visiting professor at the other institution.'},
  'FELLOW_JUDGE': {'title': 'Fellow Judge', 'desc': 'Both parties served as judges on the same federal appellate circuit.'},
  'FELLOW_JUSTICE': {'title': 'Fellow Justice', 'desc': 'Both parties served together on the U.S. Supreme Court.'},
  'FELLOW_GOVERNOR': {'title': 'Fellow Governor', 'desc': 'Both parties served as U.S. state governors.'},
}

def _get_label(node):
    if _labels and node in _labels:
        return _labels[node]
    if _canonical_map and node in _canonical_map:
        return _canonical_map[node].get("canonical", node)
    return node

_node_sci_map = None
def _get_node_sci(name: str) -> int:
    global _node_sci_map
    if _node_sci_map is None:
        _node_sci_map = {}
        if _search_index:
            for entry in _search_index:
                _node_sci_map[entry["canonical"].lower()] = entry.get("sci", 1)
    return _node_sci_map.get(name.lower(), 1)

# Confirmed, unambiguous unresolved-ID placeholder patterns leaking out of
# ingestion pipelines (whose source scripts aren't in this repo to fix at
# the root -- see the entity-type-system follow-up for the real fix).
# "FEC Campaign Committee C0000093" is the literal fallback string used
# when a committee's real name failed to resolve, and some of these have
# very high degree (e.g. C0000093 = 7136), so the degree-based score boost
# below pushes known-junk entries to the top of search results. Narrowly
# scoped to patterns confirmed 100% junk by inspection (unlike e.g. a
# trailing long digit token, which also matches real "LLC ... EIN" entity
# names and would wrongly hide legitimate results).
_PLACEHOLDER_ENTITY_PATTERNS = [
    re.compile(r"^FEC Campaign Committee( C\d+)?$"),   # unresolved FEC committee name;
                                                          # confirmed a further-degraded form
                                                          # exists too -- "FEC Campaign
                                                          # Committee " with a trailing space
                                                          # and no ID at all (empty committee_id
                                                          # in the same template), degree 39606:
                                                          # every unresolvable-even-by-ID
                                                          # committee bucketed into one node.
    re.compile(r"^Q\d{5,}$"),                            # unresolved Wikidata QID
    re.compile(r"^A\d{8,}$"),                            # unresolved SEC/IARD-style ID
]


def _is_placeholder_entity(canonical: str) -> bool:
    stripped = canonical.strip()
    return any(p.match(stripped) for p in _PLACEHOLDER_ENTITY_PATTERNS)


def _find_entry(query):
    _load_search()
    q = query.lower().strip()
    # Normalize: strip surrounding punctuation, collapse whitespace, expand
    # common suffix spellings so "donald trump junior" / "...jr," still match "Jr."
    import re as _re
    q = _re.sub(r"[,;]+", " ", q)
    q = _re.sub(r"\bjunior\b", "jr", q)
    q = _re.sub(r"\bsenior\b", "sr", q)
    q = _re.sub(r"\s+", " ", q).strip(" .,")
    results = []
    seen = set()
    if not _search_index:
        return []
    for entry in _search_index:
        canon = entry["canonical"]
        # No placeholder check here -- _search_index is already filtered
        # once at load time in _load_search().
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
            score = 92
        elif any(p.startswith(q) for p in parts):
            # A query matching the start of ANY name token (e.g. a surname)
            # is as good as matching the start of the whole string.
            score = 90
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
            # Boost well-connected hubs so the real person outranks tiny
            # same-name committees/trusts. A strong hub (high degree) can
            # cross one tier (e.g. surname-match person over a starts-with org),
            # but the cap keeps an exact full-name match (100) always on top.
            deg = entry.get("degree", 0)
            if deg > 0:
                import math
                score += min(15.0, math.log10(deg + 1) * 4.0)
            if canon_lower not in seen:
                seen.add(canon_lower)
                results.append((score, entry))
    results.sort(key=lambda x: (-x[0], -x[1].get("degree", 0), x[1]["canonical"]))
    return [r[1] for r in results[:50]]

def _viability_band(p):
    if p >= 0.5:
        return "Strong"
    if p >= 0.1:
        return "Plausible"
    if p >= 0.01:
        return "Weak"
    return "Tenuous"

def _one_in(p):
    if p <= 0:
        return "\u2248 negligible"
    if p >= 0.5:
        return f"{round(p*100)}%"
    return f"\u2248 1 in {round(1.0/p)}"

def _find_path(src_name, tgt_name, max_depth=6, k=5, include_deceased=False):
    _load_graph()
    if _graph is None:
        return {"error": "Graph not loaded"}
    src_node = _resolve_name(src_name)
    tgt_node = _resolve_name(tgt_name)
    if not src_node:
        return {"error": f"Source '{src_name}' not found"}
    if not tgt_node:
        return {"error": f"Target '{tgt_name}' not found"}

    # Living-only mode (default): deceased people cannot be INTERMEDIARIES
    # (they can't take a call or make an introduction). They are still allowed
    # as the source or target endpoint. We search a subgraph with deceased
    # intermediaries removed; the toggle (include_deceased=True) uses the full graph.
    deceased = _load_deceased()
    search_graph = _graph
    excluded_deceased = []
    if not include_deceased and deceased:
        drop = [n for n in _graph.nodes
                if n.lower() in deceased and n != src_node and n != tgt_node]
        if drop:
            search_graph = _graph.subgraph([n for n in _graph.nodes if n not in set(drop)])
            excluded_deceased = drop

    try:
        def build(path):
            # Collect per-edge take-call probabilities along the path
            edge_probs = []
            step_objects = []
            for j in range(len(path)):
                _dd = deceased.get(path[j].lower()) if deceased else None
                lbl = _get_label(path[j])
                step_objects.append({"node": path[j], "label": lbl,
                                     "relation": None, "prob": None, "cats": None,
                                     "deceased": _dd, "sci": _get_node_sci(lbl)})
            for j in range(len(path) - 1):
                ed = _graph.get_edge_data(path[j], path[j + 1]) or {}
                p = ed.get("prob", 0.5)
                cats = ed.get("cats", []) or []
                edge_probs.append(p)
                # primary label = strongest contributing category by editorial prob
                rel = None
                if cats:
                    rel = max(cats, key=lambda c: _CATEGORY_PROB.get(c, 0.0))
                elif ed.get("relation"):
                    rel = ed.get("relation")
                step_objects[j]["relation"] = rel
                step_objects[j]["prob"] = round(p, 4)
                step_objects[j]["cats"] = cats
            # Path probability = link strength * forwarding factor^(hops-1)
            path_prob, link_comp, fwd_comp = _path_probability(edge_probs)
            # Guided (Build My Path) variant: two-tier forwarding (own contact + warm intros)
            guided_prob, _gl, guided_fwd = _path_probability_guided(edge_probs)
            n_relays = max(0, len(edge_probs) - 1)
            return {
                "length": len(path) - 1,
                "probability": round(path_prob, 6),
                "link_prob": round(link_comp, 6),
                "forward_prob": round(fwd_comp, 6),
                "guided_probability": round(guided_prob, 6),
                "guided_band": _viability_band(guided_prob),
                "guided_prob_label": _one_in(guided_prob),
                "n_relays": n_relays,
                "forward_rate": _FORWARD_PROB,
                "prob_label": _one_in(path_prob),
                "band": _viability_band(path_prob),
                "path": step_objects,
            }

        # k most-probable paths = k shortest in -log(prob) weight space
        paths = []
        try:
            gen = nx.shortest_simple_paths(search_graph, src_node, tgt_node, weight="weight")
            for i, p in enumerate(gen):
                if i >= k:
                    break
                paths.append(build(p))
        except nx.NetworkXNoPath:
            return {"paths": [], "src_found": True, "tgt_found": True,
                    "deceased_excluded": len(excluded_deceased), "include_deceased": include_deceased}

        # already in decreasing-probability order from the weighted generator
        return {"paths": paths, "src_found": True, "tgt_found": True,
                "deceased_excluded": len(excluded_deceased), "include_deceased": include_deceased}
    except nx.NodeNotFound:
        return {"paths": [], "src_found": src_node is not None, "tgt_found": tgt_node is not None}
    except Exception as e:
        import traceback
        return {"error": "pathfind_failed", "detail": traceback.format_exc()[:600]}

def _resolve_name(name):
    name_lower = name.strip().lower()
    cmap = _canonical_map or {}
    for node_id, info in cmap.items():
        if info.get("canonical", "").lower() == name_lower:
            return info.get("canonical") if not info.get("is_alias") else node_id
    for node_id, info in cmap.items():
        if info.get("name", "").lower() == name_lower:
            if info.get("is_alias"):
                for nid2, info2 in cmap.items():
                    if info2.get("canonical") == info.get("canonical") and not info2.get("is_alias"):
                        return nid2
            else:
                return info.get("canonical")
    if _graph is not None and name_lower in _graph:
        return name_lower
    if _graph is not None:
        for n in _graph.nodes():
            if n.lower() == name_lower:
                return n
    return None

_evidence = None
def _load_evidence():
    global _evidence
    if _evidence is None:
        p = DATA_DIR / "evidence.json.gz"
        if p.exists():
            import gzip
            with gzip.open(p, "rt", encoding="utf-8") as f:
                _evidence = json.load(f)
        else:
            _evidence = {}
    return _evidence

# Source attribution by relation type (for bulk sources without per-edge snippets)
_REL_SOURCE = {
    "CO_DIRECTOR": ("SEC Filings (co-directorship)", "https://www.sec.gov/edgar/search/"),
    "CO_OFFICER": ("SEC Filings (co-officers)", "https://www.sec.gov/edgar/search/"),
    "DIRECTOR": ("SEC / LittleSis", "https://littlesis.org/"),
    "OFFICER": ("SEC / LittleSis", "https://littlesis.org/"),
    "CEO": ("LittleSis / Wikidata", "https://littlesis.org/"),
    "CHAIRMAN": ("LittleSis / Wikidata", "https://littlesis.org/"),
    "MEMBERSHIP": ("LittleSis", "https://littlesis.org/"),
    "DONATION": ("LittleSis (campaign finance)", "https://littlesis.org/"),
    "MENTIONED_WITH": ("GDELT Global News", "https://www.gdeltproject.org/"),
    "COMMUNICATED_WITH": ("Epstein Estate Documents", "https://oversight.house.gov/"),
    "EMPLOYER": ("Wikidata / LittleSis", "https://www.wikidata.org/"),
    "ALMA_MATER": ("Wikidata", "https://www.wikidata.org/"),
    "ALUMNI_OF": ("Wikidata / LittleSis", "https://www.wikidata.org/"),
    "BOARD_MEMBER_OF": ("LittleSis / Wikidata", "https://littlesis.org/"),
    "VISITING_PROFESSOR": ("LittleSis", "https://littlesis.org/"),
    "FELLOW_JUDGE": ("US Courts (same circuit)", "https://www.uscourts.gov/"),
    "FELLOW_JUSTICE": ("US Supreme Court", "https://www.supremecourt.gov/"),
    "FELLOW_GOVERNOR": ("Wikipedia (US governors)", "https://en.wikipedia.org/"),
    "FELLOW_REPRESENTATIVE": ("State Legislature records", ""),
    "FELLOW_SENATOR": ("State Legislature records", ""),
}

@app.get("/api/evidence")
async def get_evidence(src: str = Query(...), tgt: str = Query(...), rel: str = Query(...)):
    """Return evidence (snippets + sources) for a specific relationship edge."""
    ev_idx = _load_evidence()
    key = f"{src.lower()}|{tgt.lower()}|{rel}"
    items = ev_idx.get(key, [])
    if items:
        return {"evidence": items}
    # Fall back to relation-type attribution for bulk sources
    srcinfo = _REL_SOURCE.get(rel)
    if srcinfo:
        return {"evidence": [{"source": srcinfo[0], "snippet": "", "doc": "", "page": "", "url": srcinfo[1]}]}
    return {"evidence": []}

def _get_google_key():
    google_key = None
    env_paths = [
        Path("/c/Users/johnk/AppData/Local/hermes/.env"),
        Path("C:/Users/johnk/AppData/Local/hermes/.env"),
        Path("/c/Users/johnk/.hermes/.env"),
        DATA_DIR.parent / ".env",
        DATA_DIR.parent.parent / ".env"
    ]
    for p in env_paths:
        if p.exists():
            for line in open(p, encoding="utf-8", errors="ignore"):
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    if k.strip() == "GOOGLE_API_KEY":
                        google_key = v.strip().strip("'").strip('"')
                        break
            if google_key:
                break
                
    if not google_key:
        google_key = os.environ.get("GOOGLE_API_KEY")
    return google_key

def _generate_path_narrative(path_obj):
    """Generate a highly professional 1-paragraph narrative summary of the best path using Gemini 2.5 Flash."""
    try:
        google_key = _get_google_key()
        if not google_key:
            return None
            
        import requests
        
        # Build path context representation
        steps = path_obj["path"]
        path_chain_desc = []
        evidence_context = []
        ev_idx = _load_evidence()
        
        for j in range(len(steps)):
            label = steps[j]["label"]
            path_chain_desc.append(label)
            
            if j > 0:
                prev = steps[j-1]
                relation = prev.get("relation")
                prev_label = steps[j-1]["label"]
                
                # Fetch snippets
                key = f"{prev_label.lower()}|{label.lower()}|{relation}"
                items = ev_idx.get(key, [])
                snippets = []
                for item in items:
                    snip = item.get("snippet", "").strip()
                    src = item.get("source", "").strip()
                    if snip:
                        snippets.append(f"Source: {src} - '{snip}'")
                
                ev_str = "; ".join(snippets) if snippets else f"Extracted {relation} connection."
                evidence_context.append(f"- {prev_label} is connected to {label} via '{relation}' ({ev_str})")
                
        chain_str = " \u2192 ".join(path_chain_desc)
        evidence_str = "\n".join(evidence_context)
        
        prompt = f"""
Write a highly professional, objective 1-paragraph narrative summary explaining this relationship path.
This is for a political and business power network research intelligence tool.

Path: {chain_str}
Length: {path_obj['length']} hops
Viability success rate: {path_obj.get('guided_probability', 0) * 100:.1f}% using Build My Path

Citable source evidence for the connections:
{evidence_str}

Instructions:
1. Explain who connects to whom and WHY, referencing the specific public records (e.g. board memberships, Senate questionnaires, tax filings, Spouses, etc.) from the evidence.
2. Keep the summary strictly to **1 paragraph (max 4-5 sentences)**.
3. Be highly objective, factual, and cautious. Avoid sensationalizing or implying any misconduct or conspiratorial insinuation. If there is a family relationship (like marriage), frame it as a standard social/family tie.
4. Conclude with a practical note on the viability of this route (e.g. 'This represents a high-probability warm route because...').
5. Output ONLY the raw paragraph text. Do not wrap in markdown quotes or codeblocks.
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={google_key}"
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "text/plain"} # get raw text response
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        if response.status_code != 200:
            return None
            
        data = response.json()
        content_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        return content_text
    except Exception as e:
        print("Error generating narrative:", e)
        return None

class OPRFEvalRequest(BaseModel):
    """Opaque protocol request: deliberately contains no plaintext field."""
    model_config = ConfigDict(extra="forbid")
    key_version: int = Field(..., ge=1)
    points: list[str]

class OPRFEvalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key_version: int
    points: list[str]

@app.post("/api/contacts/oprf-eval", response_model=OPRFEvalResponse)
async def contacts_oprf_eval(body: OPRFEvalRequest, request: Request):
    """Evaluate blinded Ristretto points without receiving contact names."""
    configured_version = contact_psi_app._key_version
    if configured_version is None:
        configured_version = int(os.environ.get("CONTACT_PSI_KEY_VERSION", "1"))
    if body.key_version != configured_version:
        return JSONResponse(status_code=409, content={"detail": "unknown key version"})
    if len(body.points) > contact_psi_app.REQUEST_MAX_POINTS:
        return JSONResponse(status_code=400, content={"detail": "too many points in one request"})
    user = _request_user_email(request) or "anonymous"
    if not contact_psi_app.consume_quota(user, len(body.points)):
        return JSONResponse(status_code=429, headers={"Retry-After": "86400"}, content={"detail": "daily OPRF point quota exceeded"})
    points = []
    try:
        for encoded in body.points:
            point = base64.b64decode(encoded, validate=True)
            if len(point) != 32 or not pysodium.crypto_core_ristretto255_is_valid_point(point):
                raise ValueError("invalid point")
            points.append(point)
        evaluated = contact_psi_app.evaluate(points, body.key_version)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    return OPRFEvalResponse(key_version=body.key_version, points=[base64.b64encode(p).decode("ascii") for p in evaluated])

@app.get("/api/contacts/manifest-meta")
async def contacts_manifest_meta():
    meta = contact_psi_app.current_manifest_meta()
    if not meta:
        return JSONResponse(status_code=503, content={"detail": "contact manifest unavailable"})
    return meta

@app.get("/api/contacts/manifest-shard/{shard}")
async def contacts_manifest_shard(shard: str):
    # Strict validation before any filesystem access: `shard` must be
    # exactly the configured number of lowercase hex characters. This is
    # the only thing standing between a path parameter and a filesystem
    # path, so it must reject anything else outright (traversal, wrong
    # length, uppercase, etc.) rather than trying to sanitize it.
    expected_len = contact_psi_app.current_manifest_meta().get("shard_hex_chars", contact_psi_manifest.SHARD_HEX_CHARS)
    if not re.fullmatch(r"[0-9a-f]{%d}" % expected_len, shard):
        return JSONResponse(status_code=400, content={"detail": "invalid shard id"})
    path = DATA_DIR / "contact_psi_manifest" / contact_psi_manifest.shard_filename(shard)
    if not path.exists():
        return JSONResponse(status_code=404, content={"detail": "contact manifest shard unavailable"})
    return FileResponse(path, media_type="application/gzip", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/api/search")
async def search(request: Request, q: str = Query(default="")):
    if not q or len(q.strip()) < 2:
        return []
    _log_tester_usage(request, "search", {"query": q.strip()})
    return _find_entry(q.strip())

@app.get("/api/path")
async def path(request: Request, src_name: str = Query(default=""), tgt_name: str = Query(default=""),
               include_deceased: bool = Query(default=False)):
    if not src_name or not tgt_name:
        return {"error": "Both src_name and tgt_name required"}
    res = _find_path(src_name.strip(), tgt_name.strip(), include_deceased=include_deceased)
    _log_tester_usage(
        request,
        "path_found" if "paths" in res and len(res.get("paths", [])) > 0 else "path_not_found",
        {"src_name": src_name.strip(), "tgt_name": tgt_name.strip(), "include_deceased": include_deceased},
    )

    # Generate AI Narrative Briefing for the best path (if found)
    if "paths" in res and len(res["paths"]) > 0:
        res["narrative"] = _generate_path_narrative(res["paths"][0])
    return res

@app.get("/api/relation-info")
async def relation_info(rtype: str = Query(default="")):
    info = RELATION_INFO.get(rtype)
    if info:
        return info
    return {"title": rtype, "desc": "Relationship type from document analysis or data extraction."}

@app.get("/api/category-info")
async def category_info(cat: str = Query(default="")):
    """Per-category call-acceptance probability + how it was established."""
    label, desc = _CATEGORY_DESC.get(cat, (cat, "Uncategorized relationship; treated as weak evidence."))
    return {
        "category": cat,
        "label": label,
        "probability": _CATEGORY_PROB.get(cat, _CATEGORY_PROB.get("OTHER", 0.05)),
        "desc": desc,
    }

@app.get("/api/methodology")
async def methodology():
    return {"text": _METHODOLOGY}

# --- USER CONTRIBUTION & QUALITY CONTROL (MVP PHASE 1) ---

class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=10, max_length=15000)

@app.post("/api/extract-proof")
async def extract_proof(req: ExtractRequest):
    try:
        import requests
        # 1. Read key from .env file
        google_key = None
        env_paths = [
            Path("/c/Users/johnk/AppData/Local/hermes/.env"),
            Path("C:/Users/johnk/AppData/Local/hermes/.env"),
            Path("/c/Users/johnk/.hermes/.env"),
            DATA_DIR.parent / ".env",
            DATA_DIR.parent.parent / ".env"
        ]
        for p in env_paths:
            if p.exists():
                for line in open(p, encoding="utf-8", errors="ignore"):
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        if k.strip() == "GOOGLE_API_KEY":
                            google_key = v.strip().strip("'").strip('"')
                            break
                if google_key:
                    break
                    
        if not google_key:
            google_key = os.environ.get("GOOGLE_API_KEY")
            
        if not google_key:
            return JSONResponse(status_code=500, content={"success": False, "error": "Google API key not found in server environment."})
            
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={google_key}"
        
        prompt = f"""
Analyze the following unstructured raw text containing an AI chat transcript or proof of a connection between two entities.
Extract the core relationship connection.

Format the output strictly as a JSON object matching this schema (do NOT wrap in markdown codeblocks or quotes):
{{
  "subject": "The main person or organization A (proper capitalization, e.g. Bonnie R. Cohen)",
  "predicate": "The exact relationship predicate from this controlled list: FAMILY, FRIEND, EMPLOYMENT, CO_DIRECTOR, CO_OFFICER, CO_EXECUTIVE, MEMBERSHIP, ADVISORY, DONATION, LOBBYING, TRAVEL_MET, PUBLIC_OFFICE",
  "object": "The secondary person or organization B (proper capitalization, e.g. Louis R. Cohen)",
  "source_name": "The authoritative source publication, book, record, or site (e.g. U.S. Senate Confirmation Questionnaire)",
  "source_url": "The source URL link if mentioned in text (or empty string if not found)",
  "snippet": "A brief quote, sentence, or snippet from the text proving this connection"
}}
Ensure the returned JSON is valid and matches the fields exactly. If multiple relationships are mentioned, extract the strongest or most specific one.

Text to analyze:
---
{req.text}
---
"""
        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json"}
        }
        
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        if response.status_code != 200:
            return JSONResponse(status_code=500, content={"success": False, "error": f"Gemini API error: {response.text}"})
            
        data = response.json()
        content_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
        parsed = json.loads(content_text)
        return {"success": True, "claim": parsed}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

class TesterInviteRequest(BaseModel):
    invitee_email: str = Field(..., min_length=3, max_length=255)
    invitee_name: Optional[str] = Field(default=None, max_length=255)
    inviter_email: Optional[str] = Field(default=None, max_length=255)
    inviter_name: Optional[str] = Field(default=None, max_length=255)
    subject: Optional[str] = Field(default=None, max_length=500)
    body: Optional[str] = Field(default=None, max_length=20000)
    sent_at: Optional[str] = None

class TesterFeedbackRequest(BaseModel):
    tester_email: str = Field(..., min_length=3, max_length=255)
    feedback_text: str = Field(..., min_length=3, max_length=20000)
    source: str = Field(default="forwarded_email", max_length=50)
    subject: Optional[str] = Field(default=None, max_length=500)
    received_at: Optional[str] = None
    category: Optional[str] = Field(default=None, max_length=50)
    raw_payload: Optional[str] = Field(default=None, max_length=50000)

class TesterNoteRequest(BaseModel):
    tester_email: str = Field(..., min_length=3, max_length=255)
    note_text: str = Field(..., min_length=3, max_length=20000)
    author_email: Optional[str] = Field(default=None, max_length=255)
    source: str = Field(default="conversation_note", max_length=50)
    noted_at: Optional[str] = None

class LegalTriggerRequest(BaseModel):
    source_url: str
    
def _get_pipeline_db_path() -> str:
    path = r"C:\Users\johnk\data\pipeline_cache.db"
    if os.path.exists(path):
        return path
    local_path = os.path.join(os.path.dirname(__file__), "data", "pipeline_cache.db")
    if os.path.exists(local_path):
        return local_path
    parent_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "pipeline_cache.db")
    return parent_path

class ReviewRequest(BaseModel):
    reviewed_by: Optional[str] = None
    status: str = Field(..., description="approved, rejected, needs_review, resolved, archived")
    note: Optional[str] = None

class ResolveRequest(BaseModel):
    resolved_by: Optional[str] = None
    resolution_note: Optional[str] = None

@app.post("/api/test-department/invite-bcc")
async def td_invite_bcc(req: TesterInviteRequest):
    db_path = _test_department_db_path()
    event_id = td_record_invitation(
        db_path,
        invitee_email=req.invitee_email,
        invitee_name=req.invitee_name,
        inviter_email=req.inviter_email,
        inviter_name=req.inviter_name,
        subject=req.subject,
        body=req.body,
        sent_at=req.sent_at,
    )
    
    # SMTP Email Integration (SPEC 1)
    # Use top-level send_email import
    subject = req.subject or "Invitation to test sixdegrees.net"
    html_content = req.body or f"<p>You have been invited to test sixdegrees.net by {req.inviter_name or 'a team member'}.</p>"
    text_content = f"You have been invited to test sixdegrees.net by {req.inviter_name or 'a team member'}."
    
    res = send_email(req.invitee_email, subject, html_content, text_content)
    delivery_status = "sent" if res["success"] else "failed"
    msg_id = res.get("message_id")
    
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE testers SET invite_delivery_status = ?, invite_message_id = ? WHERE email = ?",
            (delivery_status, msg_id, req.invitee_email.strip().lower())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[invite-bcc] Error updating testers delivery status: {e}")
        
    return {
        "success": True, 
        "event_id": event_id, 
        "delivery_status": delivery_status, 
        "message_id": msg_id
    }

@app.post("/api/test-department/feedback")
async def td_feedback(req: TesterFeedbackRequest):
    feedback_id = td_add_feedback(
        _test_department_db_path(),
        tester_email=req.tester_email,
        feedback_text=req.feedback_text,
        source=req.source,
        subject=req.subject,
        received_at=req.received_at,
        category=req.category,
        raw_payload=req.raw_payload,
    )
    return {"success": True, "feedback_id": feedback_id}

@app.post("/api/test-department/note")
async def td_note(req: TesterNoteRequest):
    note_id = td_add_note(
        _test_department_db_path(),
        tester_email=req.tester_email,
        note_text=req.note_text,
        author_email=req.author_email,
        source=req.source,
        noted_at=req.noted_at,
    )
    return {"success": True, "note_id": note_id}

@app.get("/api/test-department/testers/{tester_email}")
async def td_tester_snapshot(tester_email: str):
    try:
        return td_get_tester_snapshot(_test_department_db_path(), tester_email)
    except KeyError:
        return JSONResponse(status_code=404, content={"error": "tester not found"})

@app.get("/api/test-department/summary")
async def td_summary():
    items = td_list_attention_queue(_test_department_db_path())
    counts = {}
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"counts": counts, "total": len(items), "queue": items[:25]}

@app.post("/api/legal/trigger-review")
async def trigger_legal_review(req: LegalTriggerRequest):
    # 1. Fetch live ToS (placeholder for logic)
    import requests
    try:
        r = requests.get(req.source_url, timeout=10)
        r.raise_for_status()
        raw_text = r.text
        
        # 2. Check for changes
        from webapp.legal_compliance import check_for_changes, register_obligation
        legal_db_path = DATA_DIR / "legal_compliance.db"
        test_dept_db_path = DATA_DIR / "test_department.db"
        
        change = check_for_changes(str(legal_db_path), req.source_url, raw_text)
        if change:
            # Save the current terms hash so future calls detect real changes
            register_obligation(str(legal_db_path), req.source_url, raw_text, {})
            # Also log as a legal_review item in service_items (Phase 1/2)
            try:
                conn = sqlite3.connect(str(test_dept_db_path))
                conn.execute("""
                    INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, metadata)
                    VALUES ('legal_review', 'needs_review', 'high', ?, ?, 'legal@sixdegrees.net', ?)
                """, (
                    f"Legal Compliance: Terms changed for {req.source_url}",
                    f"Obligation changes detected: {json.dumps(change)}",
                    json.dumps({"source_url": req.source_url, "change": change})
                ))
                conn.commit()
                conn.close()
            except Exception as le:
                print(f"[legal] Error logging service item: {le}")
                
            return {"success": True, "status": "change_detected", "change": change}

        return {"success": True, "status": "no_change"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class SuggestionRequest(BaseModel):
    subject: str = Field(..., min_length=2, max_length=100)
    predicate: str = Field(..., min_length=2, max_length=50)
    object: str = Field(..., min_length=2, max_length=100)
    source_name: str = Field(..., min_length=2, max_length=100)
    source_url: str = Field(...)
    snippet: str = Field(..., min_length=5, max_length=1000)
    email: Optional[str] = None

class DisputeRequest(BaseModel):
    edge_key: str = Field(..., min_length=2, max_length=250)
    reason: str = Field(..., min_length=5, max_length=1000)
    source_url: Optional[str] = None
    email: Optional[str] = None

@app.post("/api/suggest-link")
async def suggest_link(req: SuggestionRequest, request: Request):
    try:
        email = request.headers.get("Cf-Access-Authenticated-User-Email") or req.email
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 1. Legacy insert
        c.execute("""
            INSERT INTO claims (subject, predicate, object, source_name, source_url, snippet, user_email)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (req.subject.strip(), req.predicate.strip(), req.object.strip(), req.source_name.strip(), req.source_url.strip(), req.snippet.strip(), email))
        
        # 2. Unified service_items insert
        meta = json.dumps({
            "subject": req.subject.strip(),
            "predicate": req.predicate.strip(),
            "object": req.object.strip(),
            "source_name": req.source_name.strip(),
            "source_url": req.source_url.strip(),
            "snippet": req.snippet.strip()
        })
        c.execute("""
            INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, metadata)
            VALUES ('suggestion', 'new', 'normal', ?, ?, ?, ?)
        """, (f"Suggestion: {req.subject.strip()} {req.predicate.strip()} {req.object.strip()}", req.snippet.strip(), email, meta))
        
        conn.commit()
        conn.close()
        return {"success": True, "message": "Thank you! Your connection suggestion has been submitted for review."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/api/dispute-link")
async def dispute_link(req: DisputeRequest, request: Request):
    try:
        email = request.headers.get("Cf-Access-Authenticated-User-Email") or req.email
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # 1. Legacy insert
        c.execute("""
            INSERT INTO disputes (edge_key, reason, source_url, user_email)
            VALUES (?, ?, ?, ?)
        """, (req.edge_key.strip(), req.reason.strip(), req.source_url.strip() if req.source_url else None, email))
        
        # 2. Unified service_items insert
        meta = json.dumps({
            "edge_key": req.edge_key.strip(),
            "reason": req.reason.strip(),
            "source_url": req.source_url.strip() if req.source_url else None
        })
        c.execute("""
            INSERT INTO service_items (item_type, status, priority, subject, body, submitter_email, edge_key, metadata)
            VALUES ('dispute', 'new', 'normal', ?, ?, ?, ?, ?)
        """, (f"Dispute: {req.edge_key.strip()}", req.reason.strip(), email, req.edge_key.strip(), meta))
        
        conn.commit()
        conn.close()
        return {"success": True, "message": "Thank you! Your dispute/correction has been submitted for review."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/api/service/queue")
async def get_service_queue(status: Optional[str] = "new", limit: int = 25, sort: str = "created_at"):
    try:
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        if sort not in {"created_at", "priority", "id"}:
            sort = "created_at"
            
        if status:
            c.execute(f"SELECT * FROM service_items WHERE status = ? ORDER BY {sort} DESC LIMIT ?", (status, limit))
        else:
            c.execute(f"SELECT * FROM service_items ORDER BY {sort} DESC LIMIT ?", (limit,))
            
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return {"success": True, "queue": rows}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/api/service/items/{item_id}/review")
async def review_service_item(item_id: int, req: ReviewRequest, request: Request):
    try:
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        item = c.execute("SELECT * FROM service_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            conn.close()
            return JSONResponse(status_code=404, content={"success": False, "error": "Service item not found"})
            
        reviewer = req.reviewed_by or request.headers.get("Cf-Access-Authenticated-User-Email") or "admin"
        now_str = datetime.now(timezone.utc).isoformat()
        
        c.execute("""
            UPDATE service_items
            SET status = ?, reviewed_by = ?, reviewed_at = ?, resolution_note = ?
            WHERE id = ?
        """, (req.status, reviewer, now_str, req.note, item_id))
        
        conn.commit()
        conn.close()
        
        # If approved and it is a suggestion: perform Phase 3 Auto-Edge creation!
        item_type = item["item_type"]
        submitter_email = item["submitter_email"]
        
        if req.status == "approved" and item_type == "suggestion":
            meta = json.loads(item["metadata"])
            subject = meta.get("subject")
            predicate = meta.get("predicate")
            obj = meta.get("object")
            source_name = meta.get("source_name")
            source_url = meta.get("source_url")
            snippet = meta.get("snippet")
            
            # Check if subject and object exist in graph
            subject_canonical = _resolve_name(subject)
            obj_canonical = _resolve_name(obj)
            
            graph_has_nodes = False
            if subject_canonical and obj_canonical:
                graph_has_nodes = True
                try:
                    prod_db_path = _get_pipeline_db_path()
                    os.makedirs(os.path.dirname(os.path.abspath(prod_db_path)), exist_ok=True)
                    prod_conn = sqlite3.connect(prod_db_path)
                    prod_cur = prod_conn.cursor()
                    try:
                        prod_cur.execute("ALTER TABLE relationships ADD COLUMN evidence TEXT")
                    except sqlite3.OperationalError:
                        pass
                        
                    evidence_json = json.dumps([{
                        "source": source_name,
                        "url": source_url,
                        "snippet": snippet
                    }])
                    
                    tgt_type = 'PERSON' if predicate in {'FAMILY', 'COMMUNICATED_WITH', 'ASSOCIATED_WITH', 'TRANSACTED_WITH'} else 'ORG'
                    
                    prod_cur.execute("""
                        INSERT OR IGNORE INTO relationships 
                        (source_id, source_name, source_type, target_id, target_name, target_type, relation_type, source_data, evidence)
                        VALUES (NULL, ?, 'PERSON', NULL, ?, ?, ?, 'USER_SUGGESTION', ?)
                    """, (subject_canonical, obj_canonical, tgt_type, predicate, evidence_json))
                    prod_conn.commit()
                    prod_conn.close()
                except Exception as ex:
                    print(f"[review] Error writing auto-edge to pipeline_cache: {ex}")
            
            # Send notification
            # Use top-level send_email import
            email_subject = "Your connection suggestion has been approved"
            html_body = f"<p>Your suggestion to add the link between <strong>{subject}</strong> and <strong>{obj}</strong> has been approved.</p>"
            if graph_has_nodes:
                html_body += "<p>It has been successfully added to the graph.</p>"
            else:
                html_body += "<p>However, one or both of the entities were not found in the active graph, so it was queued for a future index rebuild.</p>"
            
            if submitter_email:
                send_email(submitter_email, email_subject, html_body)
                
        elif req.status == "rejected" and item_type == "suggestion":
            meta = json.loads(item["metadata"])
            subject = meta.get("subject")
            obj = meta.get("object")
            # Use top-level send_email import
            email_subject = "Your connection suggestion has been reviewed"
            html_body = f"<p>Your suggestion to add the link between <strong>{subject}</strong> and <strong>{obj}</strong> has been rejected.</p>"
            if submitter_email:
                send_email(submitter_email, email_subject, html_body)
                
        return {"success": True, "message": f"Item {item_id} reviewed successfully. Status: {req.status}"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/api/service/items/{item_id}/resolve")
async def resolve_service_item(item_id: int, req: ResolveRequest, request: Request):
    try:
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        item = c.execute("SELECT * FROM service_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            conn.close()
            return JSONResponse(status_code=404, content={"success": False, "error": "Service item not found"})
            
        resolver = req.resolved_by or request.headers.get("Cf-Access-Authenticated-User-Email") or "admin"
        now_str = datetime.now(timezone.utc).isoformat()
        
        c.execute("""
            UPDATE service_items
            SET status = 'resolved', reviewed_by = ?, reviewed_at = ?, resolution_note = ?
            WHERE id = ?
        """, (resolver, now_str, req.resolution_note, item_id))
        
        conn.commit()
        conn.close()
        
        submitter_email = item["submitter_email"]
        if submitter_email:
            # Use top-level send_email import
            email_subject = f"Service Item Resolved: {item['subject']}"
            html_body = f"<p>Your service item regarding <strong>{item['subject']}</strong> has been resolved.</p>"
            if req.resolution_note:
                html_body += f"<p>Resolution note: {req.resolution_note}</p>"
            send_email(submitter_email, email_subject, html_body)
            
        return {"success": True, "message": f"Item {item_id} resolved successfully."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.post("/api/service/notify/{item_id}")
async def notify_service_item(item_id: int):
    try:
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        item = c.execute("SELECT * FROM service_items WHERE id = ?", (item_id,)).fetchone()
        if not item:
            conn.close()
            return JSONResponse(status_code=404, content={"success": False, "error": "Service item not found"})
            
        submitter_email = item["submitter_email"]
        if not submitter_email:
            conn.close()
            return JSONResponse(status_code=400, content={"success": False, "error": "Item has no submitter email"})
            
        # Use top-level send_email import
        email_subject = f"Notification regarding service item: {item['subject']}"
        html_body = f"<p>This is a notification regarding your service item <strong>{item['subject']}</strong>.</p>"
        html_body += f"<p>Current status: <strong>{item['status']}</strong></p>"
        if item["resolution_note"]:
            html_body += f"<p>Resolution note: {item['resolution_note']}</p>"
            
        res = send_email(submitter_email, email_subject, html_body)
        conn.close()
        
        if res["success"]:
            return {"success": True, "message": f"Notification sent successfully to {submitter_email}"}
        else:
            return JSONResponse(status_code=500, content={"success": False, "error": res["error"]})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/api/service/metrics")
async def get_service_metrics():
    try:
        db_path = _test_department_db_path()
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        
        # Calculate approval rate (approved suggestions / total suggestions)
        c.execute("SELECT COUNT(*) FROM service_items WHERE item_type = 'suggestion' AND status = 'approved'")
        approved_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM service_items WHERE item_type = 'suggestion'")
        total_suggestions = c.fetchone()[0]
        
        approval_rate = (approved_count / total_suggestions) if total_suggestions > 0 else 0.0
        
        # Calculate avg resolution time in seconds
        c.execute("SELECT created_at, reviewed_at FROM service_items WHERE reviewed_at IS NOT NULL")
        times = []
        for row in c.fetchall():
            try:
                created = datetime.fromisoformat(row["created_at"])
                reviewed = datetime.fromisoformat(row["reviewed_at"])
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                if reviewed.tzinfo is None:
                    reviewed = reviewed.replace(tzinfo=timezone.utc)
                times.append((reviewed - created).total_seconds())
            except Exception:
                pass
                
        avg_res_time = (sum(times) / len(times)) if times else 0.0
        
        # Calculate per-tester metrics
        c.execute("SELECT id, email, name FROM testers")
        testers = [dict(r) for r in c.fetchall()]
        
        tester_metrics = []
        for t in testers:
            email = t["email"]
            c.execute("SELECT COUNT(*) FROM service_items WHERE submitter_email = ?", (email,))
            sub_count = c.fetchone()[0]
            
            c.execute("SELECT COUNT(*) FROM service_items WHERE submitter_email = ? AND item_type = 'suggestion' AND status = 'approved'", (email,))
            t_approved = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM service_items WHERE submitter_email = ? AND item_type = 'suggestion'", (email,))
            t_total_sug = c.fetchone()[0]
            t_app_rate = (t_approved / t_total_sug) if t_total_sug > 0 else 0.0
            
            churn_signal = False
            if sub_count >= 3:
                c.execute("SELECT MAX(event_at) FROM tester_events WHERE tester_id = ?", (t["id"],))
                last_event_at = c.fetchone()[0]
                if last_event_at:
                    try:
                        last_dt = datetime.fromisoformat(last_event_at)
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        current_dt = datetime.now(timezone.utc)
                        if (current_dt - last_dt).days >= 7:
                            churn_signal = True
                    except Exception:
                        pass
                        
            tester_metrics.append({
                "email": email,
                "name": t["name"],
                "submission_count": sub_count,
                "approval_rate": t_app_rate,
                "churn_signal": churn_signal
            })
            
        conn.close()
        return {
            "success": True,
            "metrics": {
                "approval_rate": approval_rate,
                "avg_resolution_time_seconds": avg_res_time,
                "testers": tester_metrics
            }
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": str(e)})

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(HTML_TEMPLATE)

@app.get("/faq", response_class=HTMLResponse)
async def faq_page():
    faq_path = Path(__file__).with_name("faq_v3.html")
    try:
        return HTMLResponse(faq_path.read_text(encoding="utf-8"))
    except Exception as e:
        return HTMLResponse(f"<h1>FAQ unavailable</h1><pre>{str(e)}</pre>", status_code=500)

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
  p.sub { color: #8b949e; margin-bottom: 1rem; font-size: 0.95rem; line-height: 1.5; }
  .hero-actions { display:flex; gap:10px; flex-wrap:wrap; margin-bottom: 1.25rem; }
  .hero-actions .secondary-btn { margin-top: 0; }
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
  .secondary-btn { background: #30363d; border: 1px solid #30363d; margin-top: 1rem; }
  .secondary-btn:hover { background: #484f58; border-color: #8b949e; }
  #results { margin-top: 1.5rem; }
  .path-result { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 1.5rem; margin-bottom: 1rem; position: relative; }
  .path-result .length { color: #8b949e; font-size: 0.85rem; margin-bottom: 1rem; }
  .path-header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem;
                 flex-wrap: wrap; }
  .path-rank { font-weight: 600; color: #e6edf3; font-size: 0.9rem; }
  .path-viability { font-size: 0.8rem; font-weight: 600; padding: 2px 8px;
                    border: 1px solid; border-radius: 12px; }
  .path-header .length { margin-bottom: 0; margin-left: auto; }
  .path-chain { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
  .step-rel small { color: #6e7681; font-weight: 600; }
  .path-note { color: #6e7681; font-size: 0.78rem; line-height: 1.5; margin-top: 0.5rem;
               padding: 0.75rem; background: #0d1117; border-radius: 6px; }
  .bmp-box { margin-top: 0.85rem; padding: 0.85rem; background: #0d1117;
             border: 1px solid #1f6feb44; border-radius: 8px; }
  .bmp-pitch { font-size: 0.85rem; color: #adbac7; line-height: 1.5; margin-bottom: 0.7rem; }
  .bmp-hero { display: flex; align-items: baseline; gap: 0.5rem; margin-bottom: 0.5rem; }
  .bmp-mult { font-size: 2.1rem; font-weight: 800; color: #3fb950; line-height: 1; }
  .bmp-mult-label { font-size: 0.95rem; font-weight: 600; color: #3fb950; }
  .bmp-sub { color: #6e7681; font-size: 0.8rem; }
  .bmp-btn { background: #1f6feb; color: #fff; border: none; border-radius: 6px;
             padding: 0.5rem 1rem; font-size: 0.9rem; font-weight: 600; cursor: pointer; }
  .bmp-btn:hover { background: #388bfd; }
  .bmp-steps { margin-top: 0.85rem; }
  .pb-title { font-weight: 700; color: #e6edf3; font-size: 0.95rem; margin-bottom: 0.4rem; }
  .pb-intro { font-size: 0.8rem; color: #8b949e; line-height: 1.5; margin-bottom: 0.8rem; }
  .pb-step { padding: 0.6rem 0.7rem; margin-bottom: 0.5rem; background: #161b22;
             border-left: 3px solid #1f6feb; border-radius: 4px; }
  .pb-step-h { font-size: 0.88rem; color: #e6edf3; line-height: 1.5; }
  .pb-num { display: inline-block; width: 1.4em; height: 1.4em; line-height: 1.4em;
            text-align: center; background: #1f6feb; color: #fff; border-radius: 50%;
            font-size: 0.78rem; font-weight: 700; margin-right: 0.35rem; }
  .pb-rel { font-size: 0.76rem; color: #6e7681; margin-top: 0.3rem; padding-left: 1.75em; }
  .pb-target { padding: 0.6rem 0.7rem; margin: 0.5rem 0; background: #12261a;
               border-radius: 4px; color: #3fb950; font-size: 0.9rem; }
  .pb-note { font-size: 0.78rem; color: #8b949e; line-height: 1.5; margin-top: 0.6rem;
             padding: 0.6rem; background: #161b22; border-radius: 4px; }
  .tip-prob { color: #3fb950; font-weight: 600; font-size: 0.95rem; margin: 6px 0; }
  .tip-calc { font-family: ui-monospace, monospace; font-size: 0.9rem; color: #e6edf3;
              background: #0d1117; border-radius: 6px; padding: 8px 10px; margin: 8px 0; }
  .tip-method-link { color: #58a6ff; font-size: 0.82rem; cursor: pointer; margin-top: 8px; }
  .tip-method-link:hover { text-decoration: underline; }
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
  
  /* Modal system for User Submissions */
  .modal { display: none; position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
           background: #1c2128; border: 1px solid #30363d; border-radius: 8px;
           padding: 1.5rem; max-width: 500px; width: 90%; z-index: 250; box-shadow: 0 8px 32px #00000088; }
  .modal.show { display: block; }
  .modal-overlay { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0;
                   background: rgba(0, 0, 0, 0.6); z-index: 240; }
  .modal-overlay.show { display: block; }
  .modal-title { font-weight: 600; color: #e6edf3; margin-bottom: 1rem; font-size: 1.1rem; }
  .modal-close { float: right; cursor: pointer; color: #8b949e; font-size: 1.2rem; }
  .form-group { margin-bottom: 1rem; text-align: left; }
  .form-group label { display: block; font-size: 0.85rem; color: #8b949e; margin-bottom: 0.3rem; }
  .form-group input, .form-group select, .form-group textarea {
    width: 100%; background: #0d1117; color: #e6edf3; border: 1px solid #30363d;
    border-radius: 6px; padding: 0.5rem 0.8rem; font-size: 0.9rem; outline: none; }
  .form-group input:focus, .form-group select:focus, .form-group textarea:focus { border-color: #58a6ff; }
  .modal-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1.5rem; }
  
  /* Tabs UI for suggest link modal */
  .tabs { display: flex; border-bottom: 1px solid #30363d; margin-bottom: 1rem; }
  .tab { padding: 0.5rem 1rem; cursor: pointer; color: #8b949e; border-bottom: 2px solid transparent; font-size: 0.9rem; font-weight: 600; }
  .tab.active { color: #58a6ff; border-color: #58a6ff; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
</style>
</head>
<body>
<div class="container">
  <h1>🔗 Network Pathfinder</h1>
  <p class="sub">Explore <strong>800,000+ relationships</strong> across SEC filings, Epstein documents, GDELT news, IRS foundations, Wikidata, and LittleSis. Find hidden paths between any two people or organizations. Start by entering any two people or organizations, compare the paths the system finds, then use Build My Path, Check My Contacts, and the FAQ to decide how to use the results.</p>
  <div class="hero-actions">
    <button class="secondary-btn" onclick="window.location.href='/faq'">📘 FAQ / Start Here</button>
  </div>

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

  <div style="display: flex; gap: 10px; align-items: center; margin-top: 15px;">
    <button id="find-btn" onclick="findPath()" disabled>🔍 Find Path</button>
    <button id="suggest-btn" class="secondary-btn" style="margin-top: 0; padding: 0.7rem 1.5rem;" onclick="showSuggestModal()">➕ Suggest Link</button>
  </div>
  <label style="display:block; margin-top:10px; font-size:0.82rem; color:#8b949e; cursor:pointer;">
    <input type="checkbox" id="include-deceased" onchange="if(state.src.selected&&state.tgt.selected) findPath();" style="vertical-align:middle; margin-right:6px;">
    Include deceased people as go-betweens (off by default — the dead can&rsquo;t make introductions, but may reveal historical connections)
  </label>

  <div id="results" class="results"></div>
  
  <div class="psi-section">
    <button id="psi-btn" onclick="document.getElementById('psi-file').click()">🔒 Check My Contacts</button>
    <span id="psi-result" style="display:none;"></span>
    <p class="psi-note">Your contacts are hashed in your browser and never sent in plaintext.</p>
    <input type="file" id="psi-file" accept=".vcf,.csv,.txt" style="display:none" onchange="doOPRF(this)">
  </div>

  <div class="footer-meta" style="margin-top: 2rem; border-top: 1px solid #30363d; padding-top: 1.5rem; text-align: center;">
    <div style="display:flex; gap:10px; justify-content:center; flex-wrap:wrap;">
      <button class="secondary-btn" onclick="window.open('https://docs.google.com/forms/d/e/1FAIpQLSfOR_ydz782hR27PzVrQ_xhqjl0k_ek_49c8RSuFTfp7ciP_A/viewform?usp=sf_link', '_blank')">💬 Give Feedback</button>
    </div>
    <p style="font-size: 0.8rem; color: #8b949e; margin-top: 1rem;">sixdegrees.net &bull; built for research and discovery</p>
  </div>
  
  <div id="tooltip" class="tooltip" onclick="hideTooltip()">
    <span class="close" onclick="hideTooltip()">✕</span>
    <div id="tooltip-content"></div>
  </div>

  <!-- Modal Overlay -->
  <div id="modal-overlay" class="modal-overlay" onclick="closeAllModals()"></div>

  <!-- Suggest Connection Modal -->
  <div id="suggest-modal" class="modal" style="max-width: 550px;">
    <span class="modal-close" onclick="closeAllModals()">✕</span>
    <div class="modal-title">➕ Suggest a New Connection</div>
    
    <div class="tabs">
      <div id="tab-ai" class="tab active" onclick="switchSuggestTab('ai')">🤖 Paste AI Proof (Recommended)</div>
      <div id="tab-manual" class="tab" onclick="switchSuggestTab('manual')">📝 Manual Entry</div>
    </div>
    
    <!-- Tab A: AI Extraction -->
    <div id="content-ai" class="tab-content active">
      <form id="suggest-ai-form" onsubmit="analyzeAIProof(event)">
        <div class="form-group">
          <label for="sug-ai-text">Paste Copilot, ChatGPT, or Claude conversation proof</label>
          <textarea id="sug-ai-text" required rows="6" placeholder="Paste the full chat output or proof text here. e.g. 'Louis R. Cohen is married to Bonnie R. Cohen, as verified in her Senate questionnaire...'"></textarea>
        </div>
        <button type="submit" id="ai-analyze-btn" style="width:100%; background:#1f6feb;">⚡ Analyze Proof with AI</button>
      </form>
      
      <!-- AI Parsed Results (Hidden by default, shown after API extracts) -->
      <div id="ai-parsed-results" style="display:none; margin-top:1.5rem; border-top:1px dashed #30363d; padding-top:1rem;">
        <div style="font-size:0.85rem; font-weight:600; color:#58a6ff; margin-bottom:0.8rem;">✓ Successfully Extracted (Review & edit below if needed):</div>
        <form id="suggest-form" onsubmit="submitSuggestion(event)">
          <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
            <div class="form-group">
              <label for="sug-subject">Person A (Subject)</label>
              <input type="text" id="sug-subject" required>
            </div>
            <div class="form-group">
              <label for="sug-object">Person B (Object)</label>
              <input type="text" id="sug-object" required>
            </div>
          </div>
          <div class="form-group">
            <label for="sug-predicate">Relationship (Predicate)</label>
            <select id="sug-predicate" required>
              <option value="FAMILY">Family / Spouse</option>
              <option value="FRIEND">Friend / Social</option>
              <option value="EMPLOYMENT">Employment</option>
              <option value="CO_DIRECTOR">Co-Director (Board)</option>
              <option value="CO_OFFICER">Co-Officer (Corporate)</option>
              <option value="CO_EXECUTIVE">Co-Executive</option>
              <option value="MEMBERSHIP">Shared Membership / Affiliation</option>
              <option value="ADVISORY">Advisory / Trustee</option>
              <option value="DONATION">Political Donation</option>
              <option value="LOBBYING">Lobbying</option>
              <option value="TRAVEL_MET">Travel or Documented Meeting</option>
              <option value="PUBLIC_OFFICE">Held Public Office</option>
            </select>
          </div>
          <div class="form-group">
            <label for="sug-source-name">Source Name</label>
            <input type="text" id="sug-source-name" required>
          </div>
          <div class="form-group">
            <label for="sug-source-url">Source URL (Verification Link)</label>
            <input type="url" id="sug-source-url">
          </div>
          <div class="form-group">
            <label for="sug-snippet">Supporting Snippet / Quote</label>
            <textarea id="sug-snippet" required rows="2"></textarea>
          </div>
          <div class="form-group">
            <label for="sug-email">Your Email (Optional)</label>
            <input type="email" id="sug-email" placeholder="you@example.com">
          </div>
          <div class="modal-actions">
            <button type="button" class="secondary-btn" style="margin-top:0;" onclick="closeAllModals()">Cancel</button>
            <button type="submit" id="sug-submit-btn">Submit Structured Connection</button>
          </div>
        </form>
      </div>
    </div>
    
    <!-- Tab B: Manual Entry -->
    <div id="content-manual" class="tab-content">
      <form id="suggest-manual-form" onsubmit="submitManualSuggestion(event)">
        <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px;">
          <div class="form-group">
            <label for="sug-manual-subject">Person A (Subject)</label>
            <input type="text" id="sug-manual-subject" required placeholder="e.g. Bonnie R. Cohen">
          </div>
          <div class="form-group">
            <label for="sug-manual-object">Person B (Object)</label>
            <input type="text" id="sug-manual-object" required placeholder="e.g. Louis R. Cohen">
          </div>
        </div>
        <div class="form-group">
          <label for="sug-manual-predicate">Relationship (Predicate)</label>
          <select id="sug-manual-predicate" required>
            <option value="" disabled selected>Select relationship type...</option>
            <option value="FAMILY">Family / Spouse</option>
            <option value="FRIEND">Friend / Social</option>
            <option value="EMPLOYMENT">Employment</option>
            <option value="CO_DIRECTOR">Co-Director (Board)</option>
            <option value="CO_OFFICER">Co-Officer (Corporate)</option>
            <option value="CO_EXECUTIVE">Co-Executive</option>
            <option value="MEMBERSHIP">Shared Membership / Affiliation</option>
            <option value="ADVISORY">Advisory / Trustee</option>
            <option value="DONATION">Political Donation</option>
            <option value="LOBBYING">Lobbying</option>
            <option value="TRAVEL_MET">Travel or Documented Meeting</option>
            <option value="PUBLIC_OFFICE">Held Public Office</option>
          </select>
        </div>
        <div class="form-group">
          <label for="sug-manual-source-name">Source Name</label>
          <input type="text" id="sug-manual-source-name" required placeholder="e.g. Senate Confirmation Document">
        </div>
        <div class="form-group">
          <label for="sug-manual-source-url">Source URL (Verification Link)</label>
          <input type="url" id="sug-manual-source-url" required placeholder="https://example.com/proof">
        </div>
        <div class="form-group">
          <label for="sug-manual-snippet">Supporting Snippet / Quote</label>
          <textarea id="sug-manual-snippet" required rows="2" placeholder="Exact quote proving the relationship..."></textarea>
        </div>
        <div class="form-group">
          <label for="sug-manual-email">Your Email (Optional)</label>
          <input type="email" id="sug-manual-email" placeholder="you@example.com">
        </div>
        <div class="modal-actions">
          <button type="button" class="secondary-btn" style="margin-top:0;" onclick="closeAllModals()">Cancel</button>
          <button type="submit" id="sug-manual-submit-btn">Submit Connection</button>
        </div>
      </form>
    </div>
  </div>

  <!-- Dispute Connection Modal -->
  <div id="dispute-modal" class="modal">
    <span class="modal-close" onclick="closeAllModals()">✕</span>
    <div class="modal-title">⚠️ Dispute or Correct a Link</div>
    <form id="dispute-form" onsubmit="submitDispute(event)">
      <input type="hidden" id="disp-edge-key">
      <div class="form-group">
        <label>Disputing Link:</label>
        <div id="disp-edge-display" style="font-weight:600; font-size:0.95rem; color:#58a6ff; padding: 0.3rem 0;"></div>
      </div>
      <div class="form-group">
        <label for="disp-reason">Why is this connection incorrect or misleading?</label>
        <textarea id="disp-reason" required rows="4" placeholder="Explain the error (e.g. namesakes conflated, outdated role, wrong citation)..."></textarea>
      </div>
      <div class="form-group">
        <label for="disp-source-url">Corrective Source URL (Optional verification link)</label>
        <input type="url" id="disp-source-url" placeholder="https://example.com/proof">
      </div>
      <div class="form-group">
        <label for="disp-email">Your Email (Optional, used for notification only)</label>
        <input type="email" id="disp-email" placeholder="you@example.com">
      </div>
      <div class="modal-actions">
        <button type="button" class="secondary-btn" style="margin-top:0;" onclick="closeAllModals()">Cancel</button>
        <button type="submit" id="disp-submit-btn" style="background:#d1242f;">Submit Dispute</button>
      </div>
    </form>
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
      const sciBadge = item.sci ? ' <span style="font-size:0.75rem; color:#58a6ff; background:#1f6feb22; padding:1px 5px; border-radius:10px; margin-left:5px; font-weight:600;" title="Social Capital Index (percentile rank)">' + item.sci + '</span>' : '';
      if (item.aliases && item.aliases.length > 0)
        aliasHtml = '<span class="alias">also: ' + escHtml(item.aliases.join(', ')) + '</span>';
      return '<div class="dropdown-item" data-index="' + i + '" data-name="' + escHtml(displayName) + '"'
        + ' onmousedown="selectItem(\'' + prefix + '\', ' + i + ')"'
        + ' onmouseover="highlightIdx(\'' + prefix + '\', ' + i + ')">'
        + '<span class="name">' + escHtml(displayName) + sciBadge + '</span>'
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
  const name = items[idx].getAttribute('data-name') || items[idx].querySelector('.name').childNodes[0].textContent.trim();
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
    const incDec = !!(document.getElementById('include-deceased') && document.getElementById('include-deceased').checked);
    const params = new URLSearchParams({ src_name: state.src.selected, tgt_name: state.tgt.selected });
    if (incDec) params.set('include_deceased', 'true');
    const res = await fetch('/api/path?' + params.toString(), {
      headers: { 'Accept': 'application/json' }
    });
    const data = await res.json();
    let html = '';
    if (data.error) {
      html = '<div class="error-msg">' + escHtml(data.error) + '</div>';
    } else if (!data.paths || data.paths.length === 0) {
      html = '<div class="no-path">No ' + (incDec ? '' : 'living ') + 'path found between <strong>' + escHtml(state.src.selected) + '</strong> and <strong>' + escHtml(state.tgt.selected) + '</strong>';
      if (!incDec && data.deceased_excluded > 0) {
        html += '<div style="margin-top:8px;font-size:0.85rem;">' + data.deceased_excluded + ' deceased ' + (data.deceased_excluded===1?'person was':'people were') + ' excluded as possible go-betweens. <a href="#" onclick="document.getElementById(\'include-deceased\').checked=true; findPath(); return false;" style="color:#58a6ff;">Include deceased intermediaries</a> to see historical connections.</div>';
      }
      html += '</div>';
    } else {
      if (data.narrative) {
        html += '<div class="narrative-briefing" style="background:#161b22; border:1px solid #30363d; border-left:4px solid #58a6ff; border-radius:8px; padding:1.2rem; margin-bottom:1.5rem; text-align:left;">';
        html += '  <div style="font-weight:700; color:#58a6ff; font-size:0.95rem; margin-bottom:0.6rem; display:flex; align-items:center; gap:6px;">';
        html += '    <span>📋 Executive Summary (AI GraphRAG Briefing)</span>';
        html += '  </div>';
        html += '  <div style="font-size:0.9rem; color:#c9d1d9; line-height:1.6; font-style:normal;">' + escHtml(data.narrative) + '</div>';
        html += '</div>';
      }
      data.paths.forEach((p, idx) => {
        const bandColors = {Strong:'#3fb950', Plausible:'#d29922', Weak:'#db6d28', Tenuous:'#8b949e'};
        const bc = bandColors[p.band] || '#8b949e';
        html += '<div class="path-result">';
        html += '<div class="path-header">';
        html += '<span class="path-rank">' + (idx === 0 ? 'Best path' : 'Alternate ' + idx) + '</span>';
        const vid = 'via' + Math.random().toString(36).slice(2);
        html += '<span class="path-viability" id="' + vid + '" style="color:' + bc + ';border-color:' + bc + ';cursor:pointer;" title="Click to explain">'
              + escHtml(p.band) + ' &middot; ' + escHtml(p.prob_label) + '</span>';
        html += '<span class="length">' + p.length + ' hop' + (p.length !== 1 ? 's' : '') + '</span>';
        html += '</div>';
        (function(pp, id){
          setTimeout(function(){
            const el = document.getElementById(id);
            if (el) el.onclick = function(){ showPathExplain(pp); };
          }, 0);
        })(p, vid);
        html += '<div class="path-chain">';
        p.path.forEach((step, i) => {
          if (i > 0) {
            const prev = p.path[i-1];
            const rel = prev.relation;
            const lp = prev.prob;
            html += '<span class="step-arrow">→</span>';
            if (rel) {
              const ri = Math.random().toString(36).slice(2);
              const lpct = lp != null ? Math.round(lp*100) + '%' : '';
              html += '<span class="step-rel" id="rel-' + ri + '" title="Click for sources">'
                    + escHtml(rel) + (lpct ? ' <small>' + lpct + '</small>' : '') + '</span>';
              html += '<span class="step-arrow">→</span>';
              setTimeout(() => {
                const el = document.getElementById('rel-' + ri);
                if (el) el.onclick = function(e) { showRelTooltip(e, rel, prev.label, step.label); };
              }, 0);
            }
          }
          let nodeHtml = escHtml(step.label);
          if (step.sci) {
            nodeHtml += ' <span title="Social Capital Index: ' + step.sci + ' (percentile rank)" style="font-size:0.72rem; color:#58a6ff; background:#1f6feb22; border:1px solid #1f6feb44; border-radius:10px; padding:1px 5px; margin-left:3px; font-weight:600;">' + step.sci + '</span>';
          }
          if (step.deceased) {
            nodeHtml += ' <span title="Deceased ' + escHtml(step.deceased) + ' \u2014 cannot make an introduction" style="color:#8b949e;font-size:0.8em;margin-left:3px;">\u271d</span>';
          }
          html += '<span class="step-node">' + nodeHtml + '</span>';
        });
        html += '</div>';
        // Build My Path: available for any path that has at least one intermediary
        if (p.length >= 2 && p.guided_probability != null) {
          const passPct = (p.probability*100);
          const guidPct = (p.guided_probability*100);
          const passStr = passPct >= 1 ? passPct.toFixed(0)+'%' : passPct.toFixed(1)+'%';
          const guidStr = guidPct >= 1 ? guidPct.toFixed(0)+'%' : guidPct.toFixed(1)+'%';

          const mult = (p.probability > 0) ? (p.guided_probability / p.probability) : 1;
          const multStr = mult >= 10 ? mult.toFixed(0) : mult.toFixed(1);
          const bid = 'bmp' + Math.random().toString(36).slice(2);
          html += '<div class="bmp-box">';
          html += '<div class="bmp-hero"><span class="bmp-mult">' + multStr + '\u00d7</span> <span class="bmp-mult-label">more likely to connect</span></div>';
          html += '<div class="bmp-pitch">Working this route actively with <strong>Build My Path</strong> \u2014 staying involved and getting a real introduction at each step \u2014 beats passively hoping a message gets passed along. <span class="bmp-sub">(about ' + guidStr + ' vs ' + passStr + ')</span></div>';
          html += '<button class="bmp-btn" id="' + bid + '">\ud83e\udded Build My Path</button>';
          html += '<div class="bmp-steps" id="' + bid + '-steps" style="display:none;"></div>';
          html += '</div>';
          (function(pp, id){
            setTimeout(function(){
              const b = document.getElementById(id);
              if (b) b.onclick = function(){ renderPlaybook(pp, id + '-steps'); };
            }, 0);
          })(p, bid);
        }
        html += '</div>';
      });
      html += '<div class="path-note">Viability = estimated probability the chain would pass a warm introduction at each step (each person would take the call). Multiple alternate paths shown — your own knowledge may favor a different one.</div>';
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
    const content = document.getElementById('tooltip-content');
    let html = '';
    // Lead with the call-acceptance probability for this link category
    try {
      const cres = await fetch('/api/category-info?cat=' + encodeURIComponent(rtype));
      const ci = await cres.json();
      const pct = Math.round((ci.probability || 0) * 100);
      html += '<div class="tooltip-title">' + escHtml(ci.label || rtype) + '</div>';
      html += '<div class="tip-prob">' + pct + '% &mdash; probability a phone call would be accepted on this link alone</div>';
      html += '<div class="tooltip-desc">' + escHtml(ci.desc || '') + '</div>';
    } catch(e) {
      const res = await fetch('/api/relation-info?rtype=' + encodeURIComponent(rtype));
      const info = await res.json();
      html += '<div class="tooltip-title">' + escHtml(info.title || rtype) + '</div>'
            + '<div class="tooltip-desc">' + escHtml(info.desc || '') + '</div>';
    }
    html += '<div class="tip-method-link" onclick="event.stopPropagation(); showMethodology();">How are these probabilities calculated? &rsaquo;</div>';

    // Sources & evidence
    if (src && tgt) {
      try {
        const eres = await fetch('/api/evidence?src=' + encodeURIComponent(src) + '&tgt=' + encodeURIComponent(tgt) + '&rel=' + encodeURIComponent(rtype));
        const edata = await eres.json();
        if (edata.evidence && edata.evidence.length > 0) {
          html += '<div style="margin-top:8px;border-top:1px solid #30363d;padding-top:6px;"><strong style="font-size:0.85rem;">Sources &amp; Evidence:</strong></div>';
          edata.evidence.forEach(function(ev) {
            html += '<div style="margin-top:6px;font-size:0.8rem;">';
            if (ev.source) {
              if (ev.url)
                html += '<a href="' + escHtml(ev.url) + '" target="_blank" rel="noopener" style="color:#58a6ff;text-decoration:none;">🔗 ' + escHtml(ev.source) + '</a>';
              else
                html += '<span style="color:#8b949e;">' + escHtml(ev.source) + '</span>';
            }
            if (ev.snippet) {
              html += '<div style="margin-top:3px;padding:4px 8px;background:#0d1117;border-left:2px solid #30363d;color:#c9d1d9;font-style:italic;">“' + escHtml(ev.snippet) + '”</div>';
            }
            if (ev.doc) {
              html += '<div style="color:#6e7681;font-size:0.75rem;margin-top:2px;">📄 ' + escHtml(ev.doc) + (ev.page ? ', p.' + escHtml(String(ev.page)) : '') + '</div>';
            }
            html += '</div>';
          });
        }
      } catch(e) {}
    }

    // Append Dispute/Correction link at the bottom of the tooltip
    if (src && tgt) {
      const edgeKey = src + '|' + tgt + '|' + rtype;
      html += '<div style="margin-top:12px;border-top:1px solid #30363d;padding-top:8px;text-align:right;">';
      html += '  <span class="tip-method-link" onclick="event.stopPropagation(); showDisputeModal(\'' + escHtml(edgeKey) + '\')">⚠️ Dispute / Correct this link</span>';
      html += '</div>';
    }

    content.innerHTML = html;
    document.getElementById('tooltip').classList.add('show');
  } catch(e) { console.error(e); }
}

function renderPlaybook(p, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (el.style.display !== 'none' && el.innerHTML) { el.style.display = 'none'; el.innerHTML = ''; return; }
  el.style.display = 'block';
  const nodes = p.path;
  let html = '<div class="pb-title">Your introduction plan</div>';
  html += '<div class="pb-intro">Work the chain one step at a time. After each person agrees, <strong>you</strong> personally contact the next \u2014 carrying the introduction forward. Tell each person you\u2019ll let them know once the intro is made (that accountability is what makes this work).</div>';
  for (let i = 0; i < nodes.length - 1; i++) {
    const from = nodes[i].label, to = nodes[i+1].label;
    const rel = nodes[i].relation;
    const stepNum = i + 1;
    const isFirst = (i === 0);
    html += '<div class="pb-step">';
    html += '<div class="pb-step-h"><span class="pb-num">' + stepNum + '</span> ';
    if (isFirst) {
      html += 'Contact <strong>' + escHtml(from) + '</strong> (your connection) and ask for an introduction to <strong>' + escHtml(to) + '</strong>.</div>';
    } else {
      html += 'Once introduced, reach out to <strong>' + escHtml(from) + '</strong> directly and ask them to connect you with <strong>' + escHtml(to) + '</strong>.</div>';
    }
    if (rel) html += '<div class="pb-rel">Their connection: ' + escHtml(rel.replace(/_/g,' ').toLowerCase()) + (nodes[i].prob!=null ? ' (\u2248' + Math.round(nodes[i].prob*100) + '% likely to take the call)' : '') + '</div>';
    html += '</div>';
  }
  html += '<div class="pb-target">\ud83c\udfaf Goal reached: <strong>' + escHtml(nodes[nodes.length-1].label) + '</strong></div>';
  html += '<div class="pb-note">Estimated success staying involved at each step: <strong style="color:#3fb950;">' + (p.guided_probability*100).toFixed(p.guided_probability*100>=1?0:1) + '%</strong> (vs ~' + (p.probability*100).toFixed(p.probability*100>=1?0:1) + '% if you just passed a message along and hoped). These are estimates \u2014 your own knowledge of these people matters most.</div>';
  el.innerHTML = html;
}

function showPathExplain(p) {
  const content = document.getElementById('tooltip-content');
  const start = p.path[0].label, end = p.path[p.path.length-1].label;
  const pct = (p.probability != null) ? (p.probability*100) : 0;
  const pctStr = pct >= 1 ? pct.toFixed(0) + '%' : pct.toFixed(2) + '%';
  let html = '<div class="tooltip-title">' + escHtml(p.band) + ' connection &middot; ' + escHtml(p.prob_label) + '</div>';
  html += '<div class="tooltip-desc">This is the estimated likelihood that an introduction could be passed all the way along this path \u2014 from <strong>'
        + escHtml(start) + '</strong> to <strong>' + escHtml(end) + '</strong>. It combines two things: whether each person would take the call, and whether each intermediary would actually pass you along.</div>';
  // Factor 1: link strength (product of per-edge take-call probs)
  html += '<div class="tip-calc">';
  let parts = [];
  for (let i = 0; i < p.path.length - 1; i++) {
    const lp = p.path[i].prob;
    if (lp != null) parts.push(Math.round(lp*100) + '%');
  }
  const linkPct = (p.link_prob != null) ? p.link_prob*100 : 0;
  const linkStr = linkPct >= 1 ? linkPct.toFixed(0) + '%' : linkPct.toFixed(1) + '%';
  html += '<div style="color:#8b949e;font-size:0.78rem;margin-bottom:3px;">Link strength (each step\u2019s call accepted)</div>';
  html += parts.join(' &times; ') + ' = <strong>' + linkStr + '</strong>';
  // Factor 2: forwarding (only intermediaries forward)
  if (p.n_relays > 0) {
    const fwdPct = Math.round((p.forward_rate || 0.37)*100);
    const fwdCompStr = (p.forward_prob*100).toFixed(p.forward_prob*100 >= 1 ? 0 : 1) + '%';
    html += '<div style="color:#8b949e;font-size:0.78rem;margin:8px 0 3px;">Forwarding (' + p.n_relays + ' intermedi' + (p.n_relays===1?'ary':'aries') + ' must pass you on, ' + fwdPct + '% each)</div>';
    html += Array(p.n_relays).fill(fwdPct + '%').join(' &times; ') + ' = <strong>' + fwdCompStr + '</strong>';
    html += '<div style="color:#8b949e;font-size:0.78rem;margin:8px 0 3px;">Combined</div>';
    html += linkStr + ' &times; ' + fwdCompStr + ' = <strong>' + pctStr + '</strong>';
  } else {
    html += '<div style="color:#8b949e;font-size:0.78rem;margin:8px 0 3px;">Direct connection \u2014 no intermediary needed, so no forwarding discount.</div>';
  }
  html += '</div>';
  html += '<div class="tooltip-desc" style="margin-top:6px;">A direct (1-hop) connection has no forwarding step. Each additional intermediary multiplies in a ~37% chance they\u2019ll actually make the introduction (Milgram/Watts) \u2014 which is why long chains, even strong ones, become tenuous. Click any relationship label for that link\u2019s detail.</div>';
  html += '<div class="tip-method-link" onclick="event.stopPropagation(); showMethodology();">How are these probabilities calculated? &rsaquo;</div>';
  content.innerHTML = html;
  document.getElementById('tooltip').classList.add('show');
}

async function showMethodology() {
  try {
    const res = await fetch('/api/methodology');
    const d = await res.json();
    const content = document.getElementById('tooltip-content');
    let html = '<div class="tooltip-title">How connection probabilities work</div>';
    (d.text || '').split('\n\n').forEach(function(para){
      html += '<div class="tooltip-desc" style="margin-top:8px;">' + escHtml(para) + '</div>';
    });
    content.innerHTML = html;
    document.getElementById('tooltip').classList.add('show');
  } catch(e) { console.error(e); }
}

function hideTooltip() {
  document.getElementById('tooltip').classList.remove('show');
}

/* Modal system handlers for Suggest & Dispute forms */
function showSuggestModal() {
  document.getElementById('modal-overlay').classList.add('show');
  document.getElementById('suggest-modal').classList.add('show');
}

function showDisputeModal(edgeKey) {
  document.getElementById('modal-overlay').classList.add('show');
  document.getElementById('dispute-modal').classList.add('show');
  document.getElementById('disp-edge-key').value = edgeKey;
  
  // Format edgeKey for display (e.g. "Donald Trump ↔ Bill Clinton (FAMILY)")
  const parts = edgeKey.split('|');
  if (parts.length === 3) {
    document.getElementById('disp-edge-display').textContent = parts[0] + ' \u2194 ' + parts[1] + ' (' + parts[2].replace(/_/g, ' ') + ')';
  } else {
    document.getElementById('disp-edge-display').textContent = edgeKey;
  }
}

function closeAllModals() {
  document.getElementById('modal-overlay').classList.remove('show');
  document.getElementById('suggest-modal').classList.remove('show');
  document.getElementById('dispute-modal').classList.remove('show');
  
  // Clean up any AI parsing state
  document.getElementById('ai-parsed-results').style.display = 'none';
  document.getElementById('suggest-ai-form').reset();
  document.getElementById('suggest-form').reset();
  document.getElementById('suggest-manual-form').reset();
}

function switchSuggestTab(tabName) {
  // Tabs
  document.getElementById('tab-ai').classList.toggle('active', tabName === 'ai');
  document.getElementById('tab-manual').classList.toggle('active', tabName === 'manual');
  
  // Content
  document.getElementById('content-ai').classList.toggle('active', tabName === 'ai');
  document.getElementById('content-manual').classList.toggle('active', tabName === 'manual');
}

async function analyzeAIProof(event) {
  event.preventDefault();
  const btn = document.getElementById('ai-analyze-btn');
  const oldText = btn.textContent;
  btn.textContent = '⚡ Analyzing proof with AI...';
  btn.disabled = true;
  
  const text = document.getElementById('sug-ai-text').value;
  try {
    const res = await fetch('/api/extract-proof', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text })
    });
    const d = await res.json();
    if (d.success && d.claim) {
      // Populates the structured review form
      document.getElementById('sug-subject').value = d.claim.subject || '';
      document.getElementById('sug-object').value = d.claim.object || '';
      document.getElementById('sug-predicate').value = d.claim.predicate || 'FAMILY';
      document.getElementById('sug-source-name').value = d.claim.source_name || '';
      document.getElementById('sug-source-url').value = d.claim.source_url || '';
      document.getElementById('sug-snippet').value = d.claim.snippet || '';
      
      // Reveal the review panel
      document.getElementById('ai-parsed-results').style.display = 'block';
    } else {
      alert('AI Extraction Error: ' + (d.error || 'Failed to analyze text. Please enter the details manually in Tab B.'));
    }
  } catch (e) {
    alert('Network error analyzing proof: ' + e.message);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}

async function submitSuggestion(event) {
  event.preventDefault();
  const btn = document.getElementById('sug-submit-btn');
  const oldText = btn.textContent;
  btn.textContent = '⏳ Submitting...';
  btn.disabled = true;

  const payload = {
    subject: document.getElementById('sug-subject').value,
    predicate: document.getElementById('sug-predicate').value,
    object: document.getElementById('sug-object').value,
    source_name: document.getElementById('sug-source-name').value,
    source_url: document.getElementById('sug-source-url').value,
    snippet: document.getElementById('sug-snippet').value,
    email: document.getElementById('sug-email').value || null
  };

  try {
    const res = await fetch('/api/suggest-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await res.json();
    if (d.success) {
      alert(d.message);
      closeAllModals();
    } else {
      alert('Error: ' + d.error);
    }
  } catch (e) {
    alert('Network error: ' + e.message);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}

async function submitManualSuggestion(event) {
  event.preventDefault();
  const btn = document.getElementById('sug-manual-submit-btn');
  const oldText = btn.textContent;
  btn.textContent = '⏳ Submitting...';
  btn.disabled = true;

  const payload = {
    subject: document.getElementById('sug-manual-subject').value,
    predicate: document.getElementById('sug-manual-predicate').value,
    object: document.getElementById('sug-manual-object').value,
    source_name: document.getElementById('sug-manual-source-name').value,
    source_url: document.getElementById('sug-manual-source-url').value,
    snippet: document.getElementById('sug-manual-snippet').value,
    email: document.getElementById('sug-manual-email').value || null
  };

  try {
    const res = await fetch('/api/suggest-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await res.json();
    if (d.success) {
      alert(d.message);
      closeAllModals();
    } else {
      alert('Error: ' + d.error);
    }
  } catch (e) {
    alert('Network error: ' + e.message);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}

async function submitDispute(event) {
  event.preventDefault();
  const btn = document.getElementById('disp-submit-btn');
  const oldText = btn.textContent;
  btn.textContent = '⏳ Submitting...';
  btn.disabled = true;

  const payload = {
    edge_key: document.getElementById('disp-edge-key').value,
    reason: document.getElementById('disp-reason').value,
    source_url: document.getElementById('disp-source-url').value || null,
    email: document.getElementById('disp-email').value || null
  };

  try {
    const res = await fetch('/api/dispute-link', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await res.json();
    if (d.success) {
      alert(d.message);
      document.getElementById('dispute-form').reset();
      closeAllModals();
    } else {
      alert('Error: ' + d.error);
    }
  } catch (e) {
    alert('Network error: ' + e.message);
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
}

function escHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* The contact file is parsed locally.  The only values sent below are
   blinded Ristretto points; plaintext names never enter a fetch body.
   Tier derivation (normalizeExact/phoneticKey/lshBandTokens) is a
   byte-for-byte port of webapp/contact_psi/keys.py -- verified against
   the real Python module and the pinned test vectors before landing here
   (see specifications/contact-check-psi-test-vectors.json). */

const CONTACT_PSI_SUFFIXES = new Set(['jr', 'sr', 'ii', 'iii', 'iv', 'v']);
const CONTACT_PSI_SOUNDEX = {};
for (const c of 'bfpv') CONTACT_PSI_SOUNDEX[c] = '1';
for (const c of 'cgjkqsxz') CONTACT_PSI_SOUNDEX[c] = '2';
for (const c of 'dt') CONTACT_PSI_SOUNDEX[c] = '3';
CONTACT_PSI_SOUNDEX['l'] = '4';
for (const c of 'mn') CONTACT_PSI_SOUNDEX[c] = '5';
CONTACT_PSI_SOUNDEX['r'] = '6';
for (const c of 'aeiouy') CONTACT_PSI_SOUNDEX[c] = '0';

function contactPsiNormalizeExact(name) {
  let text = String(name).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  text = text.replace(/[-'.,]/g, ' ').replace(/[^a-z0-9\s]/g, '');
  const tokens = text.split(/\s+/).filter(Boolean);
  if (tokens.length && CONTACT_PSI_SUFFIXES.has(tokens[tokens.length - 1])) tokens.pop();
  return tokens.sort().join(' ');
}

function contactPsiSoundexToken(token) {
  const letters = [...token].filter(c => c >= 'a' && c <= 'z');
  if (!letters.length) return '0000';
  const digits = [];
  let last = null;
  for (const c of letters) {
    if (c === 'h' || c === 'w') continue;
    const cls = CONTACT_PSI_SOUNDEX[c];
    if (cls !== last) {
      digits.push(cls);
      if (digits.length === 4) break;
    }
    last = cls;
  }
  while (digits.length < 4) digits.push('0');
  return digits.join('');
}

function contactPsiPhoneticKey(name) {
  const normalized = contactPsiNormalizeExact(name);
  if (!normalized) return '';
  return normalized.split(' ').map(contactPsiSoundexToken).sort().join('|');
}

function contactPsiTrigrams(name) {
  const padded = ' ' + contactPsiNormalizeExact(name) + ' ';
  const set = new Set();
  for (let i = 0; i <= padded.length - 3; i++) set.add(padded.slice(i, i + 3));
  return set;
}

function contactPsiJaccard(a, b) {
  if (!a.size && !b.size) return 1;
  let intersection = 0;
  for (const g of a) if (b.has(g)) intersection++;
  const union = a.size + b.size - intersection;
  return union ? intersection / union : 0;
}

function contactPsiFnv1a32(value) {
  let h = 0x811C9DC5;
  for (const byte of new TextEncoder().encode(value)) {
    h ^= byte;
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const CONTACT_PSI_MERSENNE_PRIME = (1n << 61n) - 1n;
// Pinned coefficient table (first 24 of the 32 in
// specifications/contact-check-psi-test-vectors.json) -- must never be
// regenerated, only copied verbatim, or phonetic/possible-tier matches
// silently stop working against the manifest built with the Python table.
const CONTACT_PSI_MINHASH_COEFFICIENTS = [
  [1979489632114620755n, 1509789760387149117n], [1918805637228309363n, 833186062390129979n],
  [495730073182853211n, 595723440359463397n], [90324890263903832n, 445813994702288431n],
  [2257285402276830348n, 1119254023105354110n], [1021786013160126921n, 1265193707630753218n],
  [212068324108876953n, 504007361524737770n], [414280352786679535n, 803222919456732142n],
  [1768409434185088727n, 1986075548297811767n], [1742620678125402464n, 2257046664463391349n],
  [1415753637546083748n, 593533786031525589n], [1196761435141869264n, 2265235534276433043n],
  [395539926857611198n, 457238027093643287n], [1503513604051188411n, 1643417863877736261n],
  [1816084546320001784n, 1408403254850552953n], [1707502431300134009n, 1007438581320924473n],
  [2225548798102152013n, 1814493586781104521n], [567344556111402844n, 1649644300899723705n],
  [463722948478937003n, 762157748086021223n], [1390024454749409324n, 597300290928022787n],
  [535273311151590049n, 445969734299799432n], [1794290647980509285n, 1609987668421870033n],
  [1379461383878485334n, 176252640438661704n], [1433166327964526570n, 1929046851930636275n],
];

function contactPsiMinhashSignature(trigramSet) {
  if (!trigramSet.size) return new Array(24).fill(0n);
  const hashes = [...trigramSet].map(g => BigInt(contactPsiFnv1a32(g)));
  return CONTACT_PSI_MINHASH_COEFFICIENTS.map(([a, b]) => {
    let m = null;
    for (const x of hashes) {
      const v = (a * x + b) % CONTACT_PSI_MERSENNE_PRIME;
      if (m === null || v < m) m = v;
    }
    return m;
  });
}

function contactPsiLshBandTokens(name) {
  const signature = contactPsiMinhashSignature(contactPsiTrigrams(name));
  const tokens = [];
  for (let i = 0; i < 3; i++) tokens.push(`${i}:` + signature.slice(i * 8, (i + 1) * 8).join(','));
  return tokens;
}

async function doOPRF(input) {
  const file = input.files[0];
  if (!file) return;
  const btn = document.getElementById('psi-btn');
  const result = document.getElementById('psi-result');
  btn.disabled = true;
  result.style.display = 'inline-block';
  result.style.color = '';
  result.textContent = '';
  try {
    const { RistrettoPoint } = await import('https://cdn.jsdelivr.net/npm/@noble/curves@1.9.7/esm/ed25519.js');
    const metaResponse = await fetch('/api/contacts/manifest-meta');
    if (!metaResponse.ok) throw new Error('Contact matching is temporarily unavailable');
    const meta = await metaResponse.json();

    const names = (await file.text()).split(/\r?\n/).map(line => line.trim())
      .filter(line => line && !/^(BEGIN|END|VERSION|EMAIL|TEL):/.test(line))
      .map(line => line.includes(':') ? line.slice(line.indexOf(':') + 1).trim() : line)
      .filter(name => name.length > 3);
    if (!names.length) throw new Error('No contacts found in file');

    const sha512 = async (bytes) => new Uint8Array(await crypto.subtle.digest('SHA-512', bytes));
    const sha256 = async (bytes) => new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    const textBytes = (s) => new TextEncoder().encode(s);
    const concatBytes = (...arrs) => { const out = new Uint8Array(arrs.reduce((n, a) => n + a.length, 0)); let o = 0; for (const a of arrs) { out.set(a, o); o += a.length; } return out; };
    const toB64 = bytes => btoa(String.fromCharCode(...bytes));
    const fromB64 = s => Uint8Array.from(atob(s), c => c.charCodeAt(0));
    const bytesToHexPrefix = (bytes, n) => Array.from(bytes.slice(0, n)).map(b => b.toString(16).padStart(2, '0')).join('');
    const leBytesToBigInt = (bytes) => { let x = 0n; for (let i = bytes.length - 1; i >= 0; i--) x = (x << 8n) | BigInt(bytes[i]); return x; };
    const randomScalar = () => RistrettoPoint.Fn.create(leBytesToBigInt(crypto.getRandomValues(new Uint8Array(32))));
    const pointFor = async (namespace, item) => RistrettoPoint.hashToCurve(await sha512(concatBytes(textBytes(namespace), new Uint8Array([0]), textBytes(item))));

    // Build every (contact, tier, item) query, keeping a back-reference to
    // the contact for possible-tier re-verification.
    const queries = [];
    for (const name of names) {
      const exact = contactPsiNormalizeExact(name);
      if (exact) queries.push({ name, tier: 'exact', item: exact });
      const phonetic = contactPsiPhoneticKey(name);
      if (phonetic) queries.push({ name, tier: 'phonetic', item: phonetic });
      for (const token of contactPsiLshBandTokens(name)) queries.push({ name, tier: 'possible', item: token });
    }

    for (const q of queries) {
      q.blindScalar = randomScalar();
      q.blindedPoint = (await pointFor(q.tier, q.item)).multiply(q.blindScalar);
    }

    const evaluatedPoints = [];
    for (let offset = 0; offset < queries.length; offset += 3000) {
      const chunk = queries.slice(offset, offset + 3000);
      const response = await fetch('/api/contacts/oprf-eval', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key_version: meta.key_version, points: chunk.map(q => toB64(q.blindedPoint.toRawBytes())) }),
      });
      if (!response.ok) throw new Error((await response.json()).detail || 'OPRF evaluation failed');
      evaluatedPoints.push(...(await response.json()).points);
    }

    // Unblind and derive the AES key for every query first, so the set of
    // manifest shards actually needed is known before fetching anything.
    const kvBytes = new Uint8Array(4);
    new DataView(kvBytes.buffer).setUint32(0, meta.key_version, false);
    for (let i = 0; i < queries.length; i++) {
      const q = queries[i];
      const responsePoint = RistrettoPoint.fromHex(fromB64(evaluatedPoints[i]));
      const rInv = RistrettoPoint.Fn.inv(q.blindScalar);
      const unblinded = responsePoint.multiply(rInv);
      q.aesKeyBytes = await sha256(concatBytes(textBytes('contact-psi-v1'), kvBytes, unblinded.toRawBytes()));
      q.prefix = bytesToHexPrefix(q.aesKeyBytes, 4);
      q.shardId = q.prefix.slice(0, meta.shard_hex_chars || 2);
    }

    // Fetch only the manifest shard(s) that cover the prefixes actually
    // derived above -- not the whole corpus. The server never learns
    // which shard(s) are requested correspond to which contact; it only
    // sees the same blinded-point traffic from the OPRF step, plus which
    // (of up to 256) shard files were requested, which reveals at most a
    // few bits of a derived key's hash prefix -- not the item itself.
    const shardUrlTemplate = meta.manifest_shard_url_template || '/api/contacts/manifest-shard/{shard}';
    const neededShardIds = [...new Set(queries.map(q => q.shardId))];
    const buckets = {};
    await Promise.all(neededShardIds.map(async (shardId) => {
      const shardResponse = await fetch(shardUrlTemplate.replace('{shard}', shardId));
      if (!shardResponse.ok) return; // shard has no entries for this prefix range -- not an error
      const shardBuf = await shardResponse.arrayBuffer();
      const decompressed = new Response(new Response(shardBuf).body.pipeThrough(new DecompressionStream('gzip')));
      const shardManifest = JSON.parse(await decompressed.text());
      Object.assign(buckets, shardManifest.buckets || {});
    }));

    const tierRank = { exact: 0, phonetic: 1, possible: 2 };
    const bestByContact = {};
    for (const q of queries) {
      const aesKeyBytes = q.aesKeyBytes;
      const entries = buckets[q.prefix] || [];
      if (!entries.length) continue;
      const cryptoKey = await crypto.subtle.importKey('raw', aesKeyBytes, 'AES-GCM', false, ['decrypt']);
      for (const entry of entries) {
        let payload;
        try {
          const pt = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: fromB64(entry.nonce) }, cryptoKey, fromB64(entry.ct));
          payload = JSON.parse(new TextDecoder().decode(pt));
        } catch (e) {
          continue; // wrong key for this bucket (prefix collision) -- expected, not an error
        }
        for (const m of payload.matches || []) {
          // Both phonetic and possible tiers are many-to-one (many
          // distinct real names can share one Soundex code or LSH band --
          // e.g. "Katharine Lee" and "Jude Law" both soundex to
          // "2030|4000", confirmed against the real ~1.46M-person corpus,
          // not a hypothetical edge case). A bundle is server-ranked by
          // degree/notability, not by resemblance to this query, so the
          // top-ranked entry in a bucket can be a totally unrelated,
          // high-degree name. Re-verify string similarity locally for
          // both tiers before ever displaying a match; exact tier needs
          // no such check since its OPRF item IS the normalized string.
          let similarity = 1;
          if (q.tier !== 'exact') {
            similarity = contactPsiJaccard(contactPsiTrigrams(q.name), contactPsiTrigrams(m.name));
            if (similarity < 0.5) continue; // phonetic/band collision, not a real resemblance
          }
          const rank = tierRank[q.tier];
          const current = bestByContact[q.name];
          if (!current || rank < current.rank || (rank === current.rank && similarity > current.similarity)) {
            bestByContact[q.name] = { rank, similarity, tier: q.tier, name: m.name, id: m.id };
          }
        }
      }
    }

    const tierLabel = { exact: 'Definite', phonetic: 'Likely', possible: 'Possible' };
    const matchedContacts = Object.keys(bestByContact);
    if (!matchedContacts.length) {
      result.textContent = `Checked ${names.length} contact${names.length === 1 ? '' : 's'} privately \u2014 no matches found`;
    } else {
      const rows = matchedContacts.map(contact => {
        const m = bestByContact[contact];
        return `<div>${escHtml(contact)} \u2192 <strong>${escHtml(m.name)}</strong> <span style="color:#8b949e">(${tierLabel[m.tier]})</span></div>`;
      }).join('');
      result.innerHTML = `<strong>${matchedContacts.length} of ${names.length} contact${names.length === 1 ? '' : 's'} found:</strong>${rows}`;
    }
  } catch (error) {
    result.textContent = 'Error: ' + error.message;
    result.style.color = '#f85149';
  } finally { btn.disabled = false; }
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
