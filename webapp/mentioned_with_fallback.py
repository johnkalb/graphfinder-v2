"""Last-resort news-co-mention lookup for pairs the main scored graph can't
connect at all.

Deliberately NOT backed by pre-harvested data: MENTIONED_WITH (GDELT news
co-mention) was dropped from graph_scored.json.gz entirely (see
build_scored_edges.py's DROP_RELATIONS) because it was 84% of raw
relationship volume and its noise was both bloating the graph past
GitHub's 100MB limit and creating fake mega-hub nodes that tanked
k-shortest-paths performance (65.4M edges -> 31s worst-case query, vs.
3.86M edges -> 4.9s once dropped). Re-admitting those edges into the graph
at a low weight would silently reintroduce the same problem: probability
only affects edge *weight* (ranking), not whether the edge -- and the
mega-hub degree that comes with it -- exists in the graph Dijkstra has to
traverse.

Instead this module is a live, on-demand check: only called when the
primary igraph search finds zero paths between two real, resolved nodes.
It queries GDELT's DOC 2.0 search API (free, no pre-harvesting, no stored
index) for articles mentioning both names, and -- capped by
llm_spend_tracker -- classifies what it finds with Haiku instead of
labeling it as an undifferentiated "News Co-mention". Results are cached
in mentioned_with_cache (DB-backed, not the in-process dict pattern
_narrative_cache uses in pathfinder.py) so a given pair is ever looked up
and classified once, and the spend cap survives App Platform redeploys
(which happen on every push to scoring-model) instead of silently
resetting.
"""
import json
import os
import datetime
import threading
import time

try:
    import db
except ImportError:
    from webapp import db

GDELT_DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
HAIKU_MODEL = "claude-haiku-4-5"
MONTHLY_CALL_CAP = 6000  # ~$18/mo at ~$0.003/call -- see cost estimate in design discussion

# Categories the classifier may pick from -- a subset of link_scoring.py's
# CATEGORY_PROB relevant to "read a news article, describe why two people
# are connected" (structured-data-only categories like SEC_INSIDER or
# PATENT_ASSIGNED_TO are excluded; those come from their own harvesters).
CLASSIFIER_CATEGORIES = {
    "FAMILY": "Family or marital relationship",
    "FRIEND": "Documented friendship",
    "COMMUNICATED": "Direct communication (call, email, letter) described",
    "EMPLOYMENT": "One employs, or is employed by, the other",
    "TRAVEL_MET": "Described as having traveled together or met in person",
    "ADVISORY": "One advises the other in an official or informal capacity",
    "MEMBERSHIP": "Both belong to the same organization, board, or group",
    "DONATION": "One donated to, or fundraised for, the other",
    "FINANCIAL": "Some other financial relationship (investment, business deal, payment)",
    "LOBBYING": "One lobbied the other or lobbied on the other's behalf",
    "PUBLIC_OFFICE": "Connected through holding public office",
    "FELLOW_OFFICEHOLDER": "Served in the same body (e.g. both governors, both judges)",
    "WEAK_SOCIAL": "Mentor, neighbor, acquaintance, or roommate",
    "NEWS_COMENTION": "No real relationship described -- just happen to both be named in the article",
}


def _pair_key(name_a, name_b):
    a, b = sorted([name_a.strip().lower(), name_b.strip().lower()])
    return f"{a}|{b}"


def _get_cached(conn, pair_key):
    cur = conn.cursor()
    cur.execute("SELECT * FROM mentioned_with_cache WHERE pair_key = ?", (pair_key,))
    row = cur.fetchone()
    if row is None:
        return None
    row = dict(row)
    return {
        "found": bool(row["found"]),
        "category": row["category"],
        "reason": row["reason"],
        "source_url": row["source_url"],
        "article_title": row["article_title"],
        "article_date": row["article_date"],
    }


def _set_cached(conn, pair_key, name_a, name_b, result):
    conn.execute(
        """INSERT OR REPLACE INTO mentioned_with_cache
           (pair_key, name_a, name_b, found, category, reason, source_url, article_title, article_date)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pair_key, name_a, name_b, int(result["found"]), result.get("category"),
         result.get("reason"), result.get("source_url"), result.get("article_title"),
         result.get("article_date")),
    )
    conn.commit()


def _check_and_increment_spend_cap(conn, monthly_cap=MONTHLY_CALL_CAP):
    """Atomically checks and increments this month's Haiku call count.
    Returns True (and has incremented) if under cap, False (unchanged) if
    the cap is already reached -- callers must still do the free GDELT
    search either way, just skip the LLM classification when this is False."""
    period = datetime.datetime.utcnow().strftime("%Y-%m")
    cur = conn.cursor()
    cur.execute("SELECT call_count FROM llm_spend_tracker WHERE period = ?", (period,))
    row = cur.fetchone()
    count = dict(row)["call_count"] if row else 0
    if count >= monthly_cap:
        return False
    if row is None:
        conn.execute("INSERT INTO llm_spend_tracker (period, call_count) VALUES (?, ?)", (period, 1))
    else:
        conn.execute("UPDATE llm_spend_tracker SET call_count = call_count + 1 WHERE period = ?", (period,))
    conn.commit()
    return True


_GDELT_MIN_INTERVAL = 5.0  # GDELT enforces "one request every 5 seconds" per caller; 429s past that
_gdelt_lock = threading.Lock()
_gdelt_last_call = [0.0]


class GdeltTransientError(Exception):
    """Rate-limited (429) or otherwise temporarily unavailable -- distinct
    from a confirmed empty result set. Callers must NOT cache this as
    found=False, or a passing rate-limit/network blip becomes a permanent
    false negative for that pair."""


def search_gdelt_comention(name_a, name_b, timeout=25, maxrecords=5):
    """Live query against GDELT's DOC 2.0 API -- no pre-harvesting, nothing
    stored ahead of time. Returns a list of {url, title, seendate} dicts,
    most recent first, or [] when GDELT genuinely returned zero articles.
    Raises GdeltTransientError on a 429/network failure so the caller can
    distinguish "confirmed no co-mention" from "couldn't check right now"
    and skip caching the latter.

    timeout=25 (was 10): GDELT's own response time has run 10-13s even on
    a successful call throughout testing, and production round-trips from
    DO's network measured right at the old 10s cutoff -- this runs off the
    hot path (async, in a thread, via /api/path/fallback, never blocking
    the main /api/path response), so there's no user-facing cost to a
    longer timeout, only to giving up too early."""
    import requests
    with _gdelt_lock:
        wait = _GDELT_MIN_INTERVAL - (time.time() - _gdelt_last_call[0])
        if wait > 0:
            time.sleep(wait)
        query = f'"{name_a}" "{name_b}"'
        params = {"query": query, "mode": "artlist", "maxrecords": maxrecords, "format": "json", "sort": "datedesc"}
        try:
            r = requests.get(GDELT_DOC_API, params=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise GdeltTransientError(f"request failed: {e}")
        finally:
            _gdelt_last_call[0] = time.time()
    if r.status_code == 429:
        raise GdeltTransientError(f"rate limited: {r.text[:200]}")
    if r.status_code != 200:
        raise GdeltTransientError(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        raise GdeltTransientError(f"bad response: {e}")
    articles = data.get("articles") or []
    return [{"url": a.get("url", ""), "title": a.get("title", ""), "seendate": a.get("seendate", "")}
            for a in articles if a.get("url")]


def _get_anthropic_key():
    return os.environ.get("ANTHROPIC_API_KEY")


def classify_comention_llm(name_a, name_b, article_title, article_url):
    """One Haiku call: given the two names and what we know about the
    article (title + URL -- no full-text fetch, keeps input tokens and
    latency small and avoids paywall/scraping failures), pick the best
    matching category from CLASSIFIER_CATEGORIES or None on any failure
    (missing API key, network error, unparseable response) -- callers
    fall back to the generic NEWS_COMENTION label in that case."""
    api_key = _get_anthropic_key()
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    category_list = "\n".join(f"- {k}: {v}" for k, v in CLASSIFIER_CATEGORIES.items())
    prompt = (
        f'Two people, "{name_a}" and "{name_b}", appear together in a news article.\n'
        f'Article title: "{article_title}"\n'
        f'Article URL: {article_url}\n\n'
        f"Based on the title and URL alone (you cannot read the full article), pick the single "
        f"best-matching category for why these two might be connected, from this list:\n"
        f"{category_list}\n\n"
        f'Respond with ONLY a JSON object, no other text: {{"category": "<ONE_OF_THE_KEYS_ABOVE>", "reason": "<one short sentence>"}}\n'
        f"If the title doesn't give enough information to tell, use NEWS_COMENTION."
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").lstrip("json").strip()
        parsed = json.loads(text)
        category = parsed.get("category")
        if category not in CLASSIFIER_CATEGORIES:
            category = "NEWS_COMENTION"
        return {"category": category, "reason": parsed.get("reason")}
    except Exception:
        return None


def get_cached_only(name_a, name_b, cache_db_path):
    """Fast, no-network cache read -- used inline in /api/path so a repeat
    query for an already-classified pair doesn't need the separate
    /api/path/fallback round-trip. Returns None on a cache miss (caller
    should then hit /api/path/fallback for the live lookup, matching how
    /api/narrative works)."""
    conn = db.connect(cache_db_path)
    try:
        return _get_cached(conn, _pair_key(name_a, name_b))
    finally:
        conn.close()


def get_fallback_path(name_a, name_b, cache_db_path):
    """Orchestrates the full last-resort lookup: cache -> live GDELT search
    -> (if under spend cap) Haiku classification -> cache the result.
    Always returns a dict with "found" plus, on a transient failure,
    "unavailable": True instead of a cached False -- a rate-limit or
    network blip must never be written to the cache as a confirmed
    negative, or that pair is wrongly marked "no relationship" forever.
    Safe to call with no ANTHROPIC_API_KEY set (classification is skipped,
    search still works)."""
    conn = db.connect(cache_db_path)
    try:
        key = _pair_key(name_a, name_b)
        cached = _get_cached(conn, key)
        if cached is not None:
            return cached

        try:
            articles = search_gdelt_comention(name_a, name_b)
        except GdeltTransientError:
            return {"found": False, "unavailable": True, "category": None, "reason": None,
                    "source_url": None, "article_title": None, "article_date": None}

        if not articles:
            result = {"found": False, "category": None, "reason": None,
                      "source_url": None, "article_title": None, "article_date": None}
            _set_cached(conn, key, name_a, name_b, result)
            return result

        top = articles[0]
        category, reason = "NEWS_COMENTION", None
        # Check for a configured key before touching the spend cap counter --
        # otherwise a deployment that's missing ANTHROPIC_API_KEY silently
        # drains the monthly cap to zero real classifications ever happening.
        if _get_anthropic_key() and _check_and_increment_spend_cap(conn):
            classified = classify_comention_llm(name_a, name_b, top["title"], top["url"])
            if classified:
                category, reason = classified["category"], classified.get("reason")

        result = {
            "found": True, "category": category, "reason": reason,
            "source_url": top["url"], "article_title": top["title"], "article_date": top.get("seendate"),
        }
        _set_cached(conn, key, name_a, name_b, result)
        return result
    finally:
        conn.close()
