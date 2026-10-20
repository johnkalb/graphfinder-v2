"""Ask a question in plain English (private-service spec Phase 3, 2026-10-20).

A small model (Haiku) reads the question and calls the site's own lookups as
tools -- paths, the user's private links, entity cards, the detail tables --
then writes a short answer from what they returned. Our code runs every
lookup, so the privacy rules stay where they already are (_detail_query's
decision-7 checks); the model never sees the database, only small, capped
tool results.

User decisions 2026-10-20: no cost estimate or approval step for now -- answer
straight away and log what each question cost; no up-front test set -- open it
to the beta testers and learn from the log (questions, tools used, cost,
feedback, and the questions we couldn't answer). Revisit the model size and the
design once there's history.

Pure functions plus the ask_log table; pathfinder.py supplies the tool
implementations (a dict name -> callable) and the HTTP layer.
"""
import html
import json
import re
import time
from datetime import datetime, timezone

try:
    import db
except ImportError:  # imported as webapp.ask_engine
    from webapp import db

DEFAULT_MODEL = "claude-haiku-5-5"
MAX_STEPS = 8            # model turns per question (each may call several tools)
MAX_TOKENS = 1200        # per model turn
MODEL_ROWS = 50          # rows of each list the model sees (the page shows more)
MODEL_CHARS = 9000       # cap on one tool result as sent to the model
PAGE_ROWS = 200          # rows of each list kept for the page

# USD per million tokens: input, output, cache read, cache write (5-minute).
# Haiku 5.5: platform.claude.com pricing, prompts up to 100K tokens (ours are
# far below that). A model missing here is logged with cost None.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": (1.00, 5.00, 0.10, 1.25),
}

# What we can answer, for the system prompt and for a user whose question we
# couldn't answer.
CAPABILITIES = [
    ("Connections", [
        "How two people or organizations are connected (\"How is Meryl Streep connected to Chuck Schumer?\")",
        "How you can reach someone through your own links (\"How do I reach Bill Gates?\")",
        "Which of your contacts are closest to a person or organization",
        "Who or what two people or organizations have in common",
    ]),
    ("Your network", [
        "Your contacts, ranked by how connected they are (SCI)",
        "Which of your contacts have a kind of tie (boards, schools, employers, donations...) or a tie to a named organization",
    ]),
    ("People and organizations", [
        "Who someone is: their strongest connections and influence score",
        "The board, officers and executives of an organization, and a public figure's roles",
        "Lobbying by or for an organization, and its lobbyists",
    ]),
    ("Political money (FEC)", [
        "The biggest donors to a candidate or committee",
        "Where a public figure's donations went",
        "PAC money in and out of a committee",
        "Who a campaign or PAC paid",
    ]),
]
NOT_AVAILABLE = ("We don't have contact details, addresses, net worth, salaries (except union officers), "
                 "news or articles, and we don't build profiles of private individuals.")


def capabilities_text():
    lines = []
    for group, items in CAPABILITIES:
        lines.append(group + ":")
        lines += ["  - " + i for i in items]
    return "\n".join(lines)


_NAME = {"type": "string", "description": "A person's or organization's name, as the user wrote it"}

TOOLS = [
    {"name": "search_names",
     "description": "Find people and organizations in the database whose names match. Use when a name is "
                    "misspelled, partial, not found, or could be several different people.",
     "input_schema": {"type": "object", "properties": {"query": _NAME}, "required": ["query"]}},
    {"name": "entity",
     "description": "Who someone is: their strongest connections (with the kind of tie), number of "
                    "connections, and SCI (1-100 influence percentile).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "path",
     "description": "The most likely chains of connections between two people or organizations in the "
                    "shared database (not using the user's private links). Up to 3 routes.",
     "input_schema": {"type": "object", "properties": {"from_name": _NAME, "to_name": _NAME},
                      "required": ["from_name", "to_name"]}},
    {"name": "my_reach",
     "description": "The best routes from the USER, through one of their own private links, to a person "
                    "or organization. Use for 'how do I reach / get to / get an intro to X'.",
     "input_schema": {"type": "object", "properties": {"target": _NAME}, "required": ["target"]}},
    {"name": "my_contacts",
     "description": "The user's own links (their contacts), with each one's SCI and whether they're in the "
                    "database, most connected first.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "my_contacts_near",
     "description": "Which of the user's contacts are directly tied to a person or organization, and which "
                    "are two steps away (with the person or organization in between).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "my_contacts_ties",
     "description": "Direct ties of the user's contacts, filtered by kinds of tie and/or by words in the "
                    "other end's name (e.g. categories [CO_DIRECTOR, ADVISORY, EMPLOYMENT] with keyword "
                    "'university'). At least one filter is required. Ask for every kind that could fit.",
     "input_schema": {"type": "object", "properties": {
         "categories": {"type": "array", "items": {"type": "string"},
                        "description": "Kinds of tie: category codes listed in the system prompt (optional)"},
         "keyword": {"type": "string", "description": "Word(s) the other end's name must contain, "
                                                      "e.g. 'hospital', 'harvard' (optional)"}}}},
    {"name": "common_connections",
     "description": "People and organizations directly tied to BOTH of two names (shared boards, schools, "
                    "employers, co-donors...).",
     "input_schema": {"type": "object", "properties": {"name_a": _NAME, "name_b": _NAME},
                      "required": ["name_a", "name_b"]}},
    {"name": "donors_to",
     "description": "Biggest itemised donors (FEC) to a candidate or committee, with amounts, cycles, "
                    "employer and occupation.",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "donations_by",
     "description": "Where a public figure's or an organization's political donations went (FEC). "
                    "Refused for private individuals.",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "pac_money",
     "description": "Money a committee or PAC gave to and received from other committees and candidates, "
                    "including independent expenditures for or against candidates (FEC).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "campaign_vendors",
     "description": "Who a campaign or PAC paid and for what, or which committees paid a vendor (FEC).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "leadership",
     "description": "Board members, officers and executives of an organization, or a public figure's "
                    "roles (IRS 990, SEC, union filings, LittleSis).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "lobbying",
     "description": "Lobbying by or for an organization (client, firm, lobbyists, issues, years) or by a "
                    "registered lobbyist (Senate LDA filings).",
     "input_schema": {"type": "object", "properties": {"name": _NAME}, "required": ["name"]}},
    {"name": "cannot_answer",
     "description": "Call this when the question needs information these tools don't have, so we can log "
                    "it and add it later. Then reply politely.",
     "input_schema": {"type": "object", "properties": {
         "reason": {"type": "string", "description": "One sentence: what's missing"},
         "closest_questions": {"type": "array", "items": {"type": "string"},
                               "description": "1-3 related questions we CAN answer, phrased for this user"}},
         "required": ["reason"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def system_prompt(owner, n_contacts, categories, today):
    cats = "\n".join(f"  {k}: {v}" for k, v in categories)
    return f"""You answer questions on sixdegrees.net, a database of documented connections between \
people and organizations (company boards, SEC filings, nonprofits' IRS filings, campaign donations, \
lobbying, schools, government posts, news co-mentions). Today is {today}.

The user is {owner}. They have {n_contacts} private links (people they know), visible only to them; \
the my_* tools use them. "I", "me" and "my" in the question mean this user.

How to answer:
- Use the tools. State only facts that come from tool results: never add names, numbers, dates or roles \
from your own knowledge. You may use general knowledge only to choose names to look up.
- If a name isn't found or could be different people, use search_names. If it's still unclear which \
one the user means, list the candidates and ask.
- If the question needs information the tools don't have, call cannot_answer, then say so politely in \
one or two sentences and suggest what you can answer instead.
- If a tool says something is withheld (private individuals' donations and roles are shown only for \
verified public figures), say so; don't try to get it another way.
- Keep the answer short: two to five sentences, or a short list of at most 10 items. The records \
you looked up are shown below your answer, so don't copy whole tables. Plain text, no markdown \
headings or tables.
- Long lists are cut to their first rows for you. Don't say how many weren't shown: the user sees the full records below your answer.
- Money: give amounts as the records state them, and say they're FEC itemised donations.
- SCI is a 1-100 percentile of how connected someone is (PageRank). A path's "odds" are our estimate \
that the chain of introductions would work.

What the tools cover:
{capabilities_text()}
{NOT_AVAILABLE}

Kinds of tie (category codes for my_contacts_ties):
{cats}"""


# --- running a question --------------------------------------------------------------

def _compact(value, rows=MODEL_ROWS):
    """A tool result as the model sees it: lists cut to `rows` with a count."""
    if isinstance(value, list):
        out = [_compact(v, rows) for v in value[:rows]]
        if len(value) > rows:
            out.append("(list continues; the user sees the rest below)")   # no count: the model kept quoting it
        return out
    if isinstance(value, dict):
        return {k: _compact(v, rows) for k, v in value.items()}
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, str) and "&" in value:
        return html.unescape(value)   # LDA filings carry "&amp;"
    return value


def _for_model(result):
    text = json.dumps(_compact(result), default=str, ensure_ascii=False)
    if len(text) > MODEL_CHARS:
        text = json.dumps(_compact(result, 8), default=str, ensure_ascii=False)[:MODEL_CHARS] + " ...(cut)"
    return text


def _rows(result):
    if isinstance(result, list):
        return len(result)
    if isinstance(result, dict):
        return sum(_rows(v) for v in result.values() if isinstance(v, (list, dict)))
    return 0


def cost_usd(model, usage):
    p = PRICES.get(model)
    if not p:
        return None
    return round((usage["input"] * p[0] + usage["output"] * p[1] + usage["cache_read"] * p[2]
                  + usage["cache_write"] * p[3]) / 1e6, 6)


def run(question, tools, *, client, model=DEFAULT_MODEL, system="", max_steps=MAX_STEPS):
    """Answer one question. `tools` maps tool name -> callable(**input) returning
    a JSON-able result (raise or return {"error": ...} on failure).

    Returns {status: answered|unanswerable|error, answer, calls, unanswerable,
    usage, cost_usd, steps, model}. calls[i] = {tool, input, result, ms, error}."""
    messages = [{"role": "user", "content": question}]
    sys_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    calls, unanswerable, answer, status = [], None, "", "answered"
    steps = 0
    for steps in range(1, max_steps + 2):
        final_turn = steps > max_steps
        kw = {"tool_choice": {"type": "none"}} if final_turn else {}
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=sys_blocks, tools=TOOLS,
                                      messages=messages, **kw)
        u = resp.usage
        usage["input"] += u.input_tokens or 0
        usage["output"] += u.output_tokens or 0
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        content, uses, text = [], [], []
        for b in resp.content:
            if b.type == "text":
                content.append({"type": "text", "text": b.text})
                text.append(b.text)
            elif b.type == "tool_use":
                content.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
                uses.append(b)
        if not uses:
            answer = "\n".join(text).strip()
            break
        messages.append({"role": "assistant", "content": content})
        results = []
        for b in uses:
            args = b.input if isinstance(b.input, dict) else {}
            t0 = time.time()
            err, result = None, None
            if b.name == "cannot_answer":
                unanswerable = {"reason": str(args.get("reason") or "")[:500],
                                "closest": [str(q)[:200] for q in (args.get("closest_questions") or [])][:3]}
                result = {"logged": True}
            elif b.name not in tools:
                err = f"unknown tool {b.name}"
            else:
                try:
                    result = tools[b.name](**args)
                except TypeError as e:
                    err = f"bad arguments: {e}"
                except Exception as e:
                    err = f"{type(e).__name__}: {str(e)[:200]}"
            if isinstance(result, dict) and result.get("error") and err is None:
                err = str(result["error"])[:300]
            ms = int((time.time() - t0) * 1000)
            if b.name != "cannot_answer":
                calls.append({"tool": b.name, "input": args, "result": _compact(result, PAGE_ROWS),
                              "ms": ms, "error": err})
            results.append({"type": "tool_result", "tool_use_id": b.id,
                            "content": json.dumps({"error": err}) if err else _for_model(result),
                            **({"is_error": True} if err else {})})
        messages.append({"role": "user", "content": results})
    if unanswerable:
        status = "unanswerable"
    if not answer:
        status, answer = "error", "Sorry, I couldn't put an answer together for that one."
    return {"status": status, "answer": answer, "calls": calls, "unanswerable": unanswerable,
            "usage": usage, "cost_usd": cost_usd(model, usage), "steps": steps, "model": model}


# --- checking the answer's numbers -----------------------------------------------------

_NUM = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:million|billion|thousand|[MBK]\b)?", re.I)
_SCALE = {"m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9, "k": 1e3, "thousand": 1e3}


def _parse(tok):
    m = re.match(r"\$?([\d,]*\.?\d+)\s*([a-z]*)", tok.strip(), re.I)
    if not m:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v * _SCALE.get(m.group(2).lower(), 1)


def _numbers_in(value, out):
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.append(float(value))
    elif isinstance(value, str):
        for tok in _NUM.findall(value):
            v = _parse(tok)
            if v is not None:
                out.append(v)
    elif isinstance(value, dict):
        for v in value.values():
            _numbers_in(v, out)
    elif isinstance(value, list):
        for v in value:
            _numbers_in(v, out)


def unverified_numbers(answer, calls):
    """Numbers (10 and up, not years) in the answer that no tool result
    contains, allowing for rounding ("$101.1M"). Logged as a flag, not
    blocked: the log tells us how often the model invents or garbles one."""
    seen = []
    for c in calls:
        _numbers_in(c.get("result"), seen)
    bad = []
    for tok in _NUM.findall(answer or ""):
        v = _parse(tok)
        if v is None or v < 10 or (1900 <= v <= 2100 and float(v).is_integer() and "$" not in tok):
            continue
        tol = max(abs(v) * 0.051, 0.5)
        if not any(abs(v - s) <= tol for s in seen) and not any(abs(v - s * 100) <= tol for s in seen):
            bad.append(tok.strip())
    return bad


# --- the ask_log table ---------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat()


def log_ask(db_path, email, question, res, ms, flags=None):
    """One row per question; returns its id."""
    u = res.get("usage") or {}
    tools = [{"tool": c["tool"], "input": c["input"], "ms": c["ms"], "rows": _rows(c.get("result")),
              "error": c.get("error"),
              "refused": bool(isinstance(c.get("result"), dict) and c["result"].get("refused"))}
             for c in res.get("calls", [])]
    una = res.get("unanswerable") or {}
    conn = db.connect(db_path)
    r = conn.execute(
        "INSERT INTO ask_log (email, asked_at, question, status, answer, reason, closest, tools, model, steps, "
        "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, ms, flags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
        (email, _now(), question, res.get("status"), res.get("answer"), una.get("reason"),
         json.dumps(una.get("closest") or []), json.dumps(tools, default=str), res.get("model"),
         res.get("steps"), u.get("input", 0), u.get("output", 0), u.get("cache_read", 0),
         u.get("cache_write", 0), res.get("cost_usd"), int(ms), json.dumps(flags or {}))).fetchone()
    conn.commit()
    conn.close()
    return r["id"]


def asked_today(db_path, email):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = db.connect(db_path)
    r = conn.execute("SELECT count(*) AS n FROM ask_log WHERE email = ? AND asked_at >= ?", (email, day)).fetchone()
    conn.close()
    return int(r["n"])


def month_cost(db_path):
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = db.connect(db_path)
    r = conn.execute("SELECT coalesce(sum(cost_usd), 0) AS c FROM ask_log WHERE asked_at >= ?", (month,)).fetchone()
    conn.close()
    return float(r["c"] or 0)


def set_feedback(db_path, email, ask_id, helpful, note=None):
    conn = db.connect(db_path)
    r = conn.execute("SELECT id FROM ask_log WHERE id = ? AND email = ?", (ask_id, email)).fetchone()
    if r:
        conn.execute("UPDATE ask_log SET feedback = ?, feedback_note = ? WHERE id = ?",
                     (1 if helpful else -1, (note or "")[:1000] or None, ask_id))
        conn.commit()
    conn.close()
    return r is not None


def recent(db_path, since_iso, limit=500):
    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT id, email, asked_at, question, status, answer, reason, closest, tools, model, steps, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, ms, flags, feedback, feedback_note "
        "FROM ask_log WHERE asked_at >= ? ORDER BY id DESC LIMIT ?", (since_iso, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("closest", "tools", "flags"):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except ValueError:
                pass
        out.append(d)
    return out


def anonymize(db_path, email):
    """Account deletion (decision 10): logs stay, without the email."""
    conn = db.connect(db_path)
    conn.execute("UPDATE ask_log SET email = NULL WHERE email = ?", (email,))
    conn.commit()
    conn.close()
