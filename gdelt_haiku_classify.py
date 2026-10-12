#!/usr/bin/env python3
"""GDELT co-occurrence pairs -> person-to-person links, read by Claude Haiku
(2026-10-12).

Why: the REBEL classifier (gdelt_classify_new_people.py) promotes <1% of
pairs because REBEL emits Wikidata-style person->entity facts ("position
held", "cast member of Friends") and almost never a person->person triplet
-- a 40-pair rescore of rejected pairs found zero direct triplets. Haiku is
asked the actual question instead: according to these excerpts, how are A
and B connected, if at all?

Scope (user decision 2026-10-12): unreviewed pairs with occurrence_count >=
MIN_MENTIONS, >=2 stored URLs, both names graph people (graph_people.tsv.gz),
and not already joined by a direct graph edge with p >= SKIP_IF_EDGE_P.
Hard spend cap SPEND_CAP_USD across all runs, enforced before every batch
submission from (actual cost so far + worst case of batches in flight + worst
case of the new batch), priced at the standard (non-batch) rate so the cap is
conservative even though the Message Batches API bills half that.

State lives in Postgres table gdelt_haiku_review (one row per pair; stage =
selected -> fetched | no_text -> submitted -> done). Selected pairs get
gdelt_cooccurrence_evidence.promotion_status = 'haiku_queue' so the REBEL
classifier skips them; the final status is promoted / rejected / no_articles.
Promoted links are written with source_data = 'GDELT_HAIKU' and only when
Haiku's supporting quote appears verbatim in the excerpts it was given.

Usage: python gdelt_haiku_classify.py select | fetch | submit | collect | run
          [--limit N]   (run = select once, then loop fetch/submit/collect
                         until every selected pair is done or the cap stops it)
Secrets come from pg_secret (DATABASE_URL, ANTHROPIC_API_KEY); runs on the
Windows box so the API key never leaves it."""
import argparse
import concurrent.futures as cf
import gzip
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
from pg_secret import load_secret  # noqa: E402

os.environ.setdefault("DATABASE_URL", load_secret("DATABASE_URL"))
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import gdelt_classify_new_people as rebel_job  # noqa: E402  (canon_key, pick_urls, load_graph_people)
from src.data.db_manager import DBManager  # noqa: E402
from src.config import DB_PATH  # noqa: E402

MODEL = "claude-haiku-5-5"
MIN_MENTIONS = 100
SKIP_IF_EDGE_P = 0.5
SPEND_CAP_USD = 400.0
# Conservative accounting rates (standard API, prompts <100K tokens). The
# Batch API bills half of these; the cap is measured against these anyway.
PRICE_IN_PER_TOK = 0.10 / 1_000_000
PRICE_OUT_PER_TOK = 0.50 / 1_000_000
MAX_OUT_TOKENS = 400
BATCH_SIZE = 5000
FETCH_WORKERS = 96
FETCH_CHUNK = 2000
# 6 articles + neighbouring paragraphs (user decision 2026-10-12, after a
# 3-article pilot lost relationships stated just before/after a name)
MAX_DOCS_WITH_BOTH = 6
EXCERPT_CHARS_PER_DOC = 3000
EXCERPT_CHARS_TOTAL = 18000
GRAPH_JSON = os.path.join(HERE, "webapp", "data", "graph_scored.json.gz")
LOG_FILE = os.path.join(HERE, "logs", "gdelt_haiku_classify.log")

RELATIONS = {
    # Haiku label -> relation_type written to `relationships` (all map to a
    # person-to-person category in webapp/relation_categories.categorize and
    # none is in build_scored_edges.POS_RELS, which drops person-person rows)
    "SPOUSE": "SPOUSE", "FAMILY": "FAMILY", "ROMANTIC_PARTNER": "ROMANTIC_PARTNER",
    "FRIEND": "FRIEND", "WORKS_FOR": "STAFF", "ADVISOR": "ADVISOR",
    "BUSINESS_PARTNER": "BUSINESS_PARTNER", "WORKED_WITH": "WORKED_WITH",
    "CO_STAR": "CO_STAR", "MET_WITH": "MET_WITH", "LAWYER_CLIENT": "ATTORNEY",
    "MENTOR": "MENTOR",
}

SYSTEM_PROMPT = """You read news excerpts and decide whether they show that two specific people have a direct personal or working relationship -- that they actually know each other or have dealt with each other.

Answer with ONE label:
SPOUSE - married (current or former)
FAMILY - parent, child, sibling, or other relative
ROMANTIC_PARTNER - dating, engaged, partners
FRIEND - described as friends
WORKS_FOR - one works for, serves under, was appointed/hired by, or is an aide/staffer to the other
ADVISOR - one advises or counsels the other
BUSINESS_PARTNER - co-founded or co-run a business or deal together
WORKED_WITH - colleagues on the same team, campaign ticket, administration, firm, band, or sports team at the same time
CO_STAR - acted or performed together in the same production
MET_WITH - met in person, held talks, negotiated face to face
LAWYER_CLIENT - one is the other's lawyer
MENTOR - one mentored or coached the other
NONE - anything else

Use NONE when they are only mentioned in the same story, are opponents or rivals, held the same office at different times, belong to the same party or organization without working together, comment on or criticize each other from a distance, or when the excerpts are unclear. Judge only from the excerpts, not from what you know about these people. When unsure, answer NONE.

The quote must be one sentence, or two consecutive sentences, copied exactly from the excerpts, that name BOTH people (full name or surname) and show the connection. If no such passage exists, answer NONE.

Record your answer with the record_verdict tool: label, quote (the passage copied exactly, or empty for NONE), and confidence."""

# Forced tool call: the pilot's free-text JSON replies sometimes came wrapped
# in reasoning prose, or as two JSON objects with a self-correction.
VERDICT_TOOL = {
    "name": "record_verdict",
    "description": "Record how the two people are connected according to the excerpts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": sorted(RELATIONS) + ["NONE"]},
            "quote": {"type": "string"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["label", "quote", "confidence"],
    },
}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_table(db):
    cur = db.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS gdelt_haiku_review (
        pair_hash TEXT PRIMARY KEY, name_a TEXT NOT NULL, name_b TEXT NOT NULL,
        occurrence_count INTEGER, urls TEXT[], stage TEXT NOT NULL DEFAULT 'selected',
        excerpts TEXT, excerpt_urls TEXT[], batch_id TEXT, label TEXT, relation_type TEXT,
        quote TEXT, quote_url TEXT, confidence TEXT, verdict TEXT, raw_reply TEXT,
        input_tokens INTEGER, output_tokens INTEGER, cost_usd NUMERIC(12, 8),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    cur.execute("CREATE INDEX IF NOT EXISTS gdelt_haiku_review_stage ON gdelt_haiku_review (stage)")
    cur.execute("""CREATE TABLE IF NOT EXISTS gdelt_haiku_batches (
        batch_id TEXT PRIMARY KEY, n_requests INTEGER, worst_case_usd NUMERIC(12, 6),
        status TEXT NOT NULL DEFAULT 'in_progress', submitted_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    db.commit()


# ---------------------------------------------------------------- select

def strong_edge_pairs(cand_keys):
    """Canon-key pairs (sorted tuple) among cand_keys already joined by a
    direct graph edge with p >= SKIP_IF_EDGE_P."""
    g = json.load(gzip.open(GRAPH_JSON, "rt", encoding="utf-8"))
    key_of = {}
    for i, name in enumerate(g["nodes"]):
        k = rebel_job.canon_key(name)
        if k in cand_keys:
            key_of[i] = k
    for i, base in g.get("splits", {}).items():
        k = rebel_job.canon_key(base)
        if k in cand_keys:
            key_of[int(i)] = k
    for i, alts in g.get("aliases", {}).items():
        if int(i) in key_of:
            continue
        for alt in alts:
            k = rebel_job.canon_key(alt)
            if k in cand_keys:
                key_of[int(i)] = k
                break
    out = set()
    for u, v, p, _ in g["edges"]:
        if p >= SKIP_IF_EDGE_P and u in key_of and v in key_of:
            out.add(tuple(sorted((key_of[u], key_of[v]))))
    del g
    return out


def select(db, limit=None):
    people = rebel_job.load_graph_people()
    log(f"select: {len(people):,} graph people")
    cur = db.cursor(name="sel")
    cur.itersize = 20000
    cur.execute("SELECT pair_hash, name_a, name_b, occurrence_count, sample_urls, sample_days "
                "FROM gdelt_cooccurrence_evidence WHERE promotion_status = 'unreviewed' "
                "AND occurrence_count >= %s AND cardinality(sample_urls) >= 2 "
                "ORDER BY occurrence_count DESC", (MIN_MENTIONS,))
    cands = []
    for h, a, b, n, urls, days in cur:
        ka, kb = rebel_job.canon_key(a), rebel_job.canon_key(b)
        if ka in people and kb in people and ka != kb:
            cands.append((h, ka, kb, people[ka], people[kb], n, rebel_job.pick_urls(urls, days)))
    cur.close()
    log(f"select: {len(cands):,} unreviewed pairs with >= {MIN_MENTIONS} mentions, both graph people")
    linked = strong_edge_pairs({c[1] for c in cands} | {c[2] for c in cands})
    keep = [c for c in cands if tuple(sorted((c[1], c[2]))) not in linked]
    log(f"select: {len(cands) - len(keep):,} already have a direct edge p >= {SKIP_IF_EDGE_P}; {len(keep):,} to review")
    if limit:
        keep = keep[:limit]
    w = db.cursor()
    psycopg2.extras.execute_values(
        w, "INSERT INTO gdelt_haiku_review (pair_hash, name_a, name_b, occurrence_count, urls) VALUES %s "
           "ON CONFLICT (pair_hash) DO NOTHING",
        [(h, da, dbb, n, urls) for h, _, _, da, dbb, n, urls in keep], page_size=5000)
    db.commit()
    # The GDELT harvester updates these rows continuously; one 196K-row UPDATE
    # deadlocked with it (2026-10-12), so mark them in small sorted chunks.
    hashes = sorted(c[0] for c in keep)
    for i in range(0, len(hashes), 1000):
        retry_on_deadlock(db, lambda cur, chunk=hashes[i:i + 1000]: cur.execute(
            "UPDATE gdelt_cooccurrence_evidence SET promotion_status = 'haiku_queue' "
            "WHERE pair_hash = ANY(%s) AND promotion_status = 'unreviewed'", (chunk,)))
    log(f"select: queued {len(keep):,} pairs")


def set_evidence_status(db, by_status):
    """{status: [pair_hash]} -> gdelt_cooccurrence_evidence, in small sorted
    chunks committed separately (see select())."""
    for status, hashes in by_status.items():
        hashes = sorted(hashes)
        for i in range(0, len(hashes), 1000):
            retry_on_deadlock(db, lambda cur, chunk=hashes[i:i + 1000], st=status: cur.execute(
                "UPDATE gdelt_cooccurrence_evidence SET promotion_status = %s WHERE pair_hash = ANY(%s)", (st, chunk)))


def retry_on_deadlock(db, fn, tries=8):
    """Run fn(cursor) and commit, retrying when Postgres picks this
    transaction as a deadlock victim."""
    for attempt in range(tries):
        try:
            fn(db.cursor())
            db.commit()
            return
        except (psycopg2.errors.DeadlockDetected, psycopg2.errors.LockNotAvailable):
            db.rollback()
            time.sleep(2 + 3 * attempt)
    raise RuntimeError(f"still deadlocking after {tries} tries")


# ---------------------------------------------------------------- fetch

_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def name_terms(name):
    """Lowercase strings that count as a mention: the full name and the
    surname (last token that isn't a suffix)."""
    toks = [t for t in re.split(r"[\s.,]+", name.lower()) if t]
    sur = next((t for t in reversed(toks) if t not in _SUFFIX), toks[-1] if toks else "")
    return {name.lower(), sur} if len(sur) >= 3 else {name.lower()}


def fetch_text(url):
    import requests
    import trafilatura
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (sixdegrees research)"})
        if r.status_code != 200 or not r.text:
            return None
        return trafilatura.extract(r.text)
    except Exception:
        return None


def excerpt(text, ta, tb):
    """Paragraphs mentioning both people first, then either, each with the
    paragraph before and after it (pronoun context: "the guard", "he"), in
    article order, up to EXCERPT_CHARS_PER_DOC. None if the article never
    names both."""
    low = text.lower()
    if not (any(t in low for t in ta) and any(t in low for t in tb)):
        return None
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    both = [i for i, p in enumerate(paras) if any(t in p.lower() for t in ta) and any(t in p.lower() for t in tb)]
    one = [i for i, p in enumerate(paras) if i not in both and any(t in p.lower() for t in ta | tb)]
    picked, size = set(), 0
    for i in both + one:
        window = [j for j in (i, i - 1, i + 1) if 0 <= j < len(paras) and j not in picked]
        for j in window:
            if size + len(paras[j]) > EXCERPT_CHARS_PER_DOC and picked:
                continue
            picked.add(j)
            size += len(paras[j])
    return "\n".join(paras[i][:EXCERPT_CHARS_PER_DOC] for i in sorted(picked))


def build_excerpts(row):
    h, a, b, urls = row
    ta, tb = name_terms(a), name_terms(b)
    parts, used, seen_texts = [], [], []
    for url in urls:
        text = fetch_text(url)
        if not text:
            continue
        ex = excerpt(text, ta, tb)
        if not ex or any(ex[:300] == s[:300] for s in seen_texts):   # syndicated copy
            continue
        seen_texts.append(ex)
        used.append(url)
        parts.append(f"[Article {len(used)}] {url}\n{ex}")
        if len(used) >= MAX_DOCS_WITH_BOTH or sum(map(len, parts)) >= EXCERPT_CHARS_TOTAL:
            break
    return h, ("\n\n".join(parts)[:EXCERPT_CHARS_TOTAL] if parts else None), used


def fetch(db, limit=None):
    cur = db.cursor()
    cur.execute("SELECT pair_hash, name_a, name_b, urls FROM gdelt_haiku_review WHERE stage = 'selected' "
                "ORDER BY occurrence_count DESC LIMIT %s", (limit or FETCH_CHUNK,))
    rows = cur.fetchall()
    if not rows:
        return 0
    t0 = time.time()
    with cf.ThreadPoolExecutor(FETCH_WORKERS) as ex:
        results = list(ex.map(build_excerpts, rows))
    w = db.cursor()
    for h, text, used in results:
        if text:
            w.execute("UPDATE gdelt_haiku_review SET stage='fetched', excerpts=%s, excerpt_urls=%s, updated_at=now() "
                      "WHERE pair_hash=%s", (text, used, h))
        else:
            w.execute("UPDATE gdelt_haiku_review SET stage='done', verdict='no_text', updated_at=now() WHERE pair_hash=%s", (h,))
    db.commit()
    set_evidence_status(db, {"no_articles": [h for h, t, _ in results if not t]})
    n_ok = sum(1 for _, t, _ in results if t)
    log(f"fetch: {len(rows)} pairs, {n_ok} with usable excerpts, {len(rows) - n_ok} without ({time.time() - t0:.0f}s)")
    return len(rows)


# ---------------------------------------------------------------- submit / collect

def anthropic_client():
    import anthropic
    return anthropic.Anthropic(api_key=load_secret("ANTHROPIC_API_KEY"))


def user_prompt(a, b, excerpts):
    return f"Person A: {a}\nPerson B: {b}\n\nExcerpts:\n{excerpts}\n\nHow are {a} and {b} connected, according to these excerpts?"


def spend_state(db):
    cur = db.cursor()
    cur.execute("SELECT coalesce(sum(cost_usd), 0) FROM gdelt_haiku_review")
    actual = float(cur.fetchone()[0])
    cur.execute("SELECT coalesce(sum(worst_case_usd), 0) FROM gdelt_haiku_batches WHERE status = 'in_progress'")
    pending = float(cur.fetchone()[0])
    return actual, pending


def submit(db, client, limit=None):
    """Submit up to one batch of fetched pairs if the cap allows. Returns the
    number submitted, or -1 if the cap stopped it."""
    cur = db.cursor()
    cur.execute("SELECT pair_hash, name_a, name_b, excerpts FROM gdelt_haiku_review WHERE stage = 'fetched' "
                "ORDER BY occurrence_count DESC LIMIT %s", (limit or BATCH_SIZE,))
    rows = cur.fetchall()
    if not rows:
        return 0
    reqs, worst = [], 0.0
    for h, a, b, ex in rows:
        prompt = user_prompt(a, b, ex)
        # ~3.5 chars/token for English; +25% margin on top for safety
        est_in = int((len(SYSTEM_PROMPT) + len(prompt)) / 3.5 * 1.25)
        worst += est_in * PRICE_IN_PER_TOK + MAX_OUT_TOKENS * PRICE_OUT_PER_TOK
        reqs.append({"custom_id": h, "params": {
            "model": MODEL, "max_tokens": MAX_OUT_TOKENS, "system": SYSTEM_PROMPT,
            # Haiku 5.5 thinks by default; on a 400-token budget that left 7 of
            # the 40 pilot replies with no text at all (2026-10-12).
            "thinking": {"type": "disabled"},
            "tools": [VERDICT_TOOL], "tool_choice": {"type": "tool", "name": "record_verdict"},
            "messages": [{"role": "user", "content": prompt}]}})
    actual, pending = spend_state(db)
    if actual + pending + worst > SPEND_CAP_USD:
        log(f"submit: STOP -- spent ${actual:.2f} + in flight ${pending:.2f} + this batch ${worst:.2f} "
            f"would exceed the ${SPEND_CAP_USD:.0f} cap")
        return -1
    batch = client.messages.batches.create(requests=reqs)
    w = db.cursor()
    w.execute("INSERT INTO gdelt_haiku_batches (batch_id, n_requests, worst_case_usd) VALUES (%s, %s, %s)",
              (batch.id, len(reqs), worst))
    psycopg2.extras.execute_values(
        w, "UPDATE gdelt_haiku_review r SET stage='submitted', batch_id=v.b, updated_at=now() FROM (VALUES %s) v(h, b) "
           "WHERE r.pair_hash = v.h",
        [(r[0], batch.id) for r in rows], page_size=5000)
    db.commit()
    log(f"submit: batch {batch.id} with {len(reqs)} requests (worst case ${worst:.2f}; spent ${actual:.2f}, "
        f"in flight ${pending:.2f})")
    return len(reqs)


_WS = re.compile(r"\s+")


def _norm(s):
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return _WS.sub(" ", s).strip().lower()


def parse_reply(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def judge(reply, excerpts, name_a, name_b):
    """-> (verdict, label, relation_type, quote, quote_url). The quote must
    appear verbatim in the excerpts AND name both people -- in the pilot,
    quotes like "Gerrit Cole turned in his usual quality start" carried a
    (true) WORKED_WITH for Boone/Cole that the excerpt didn't show."""
    if not reply:
        return "bad_reply", None, None, None, None
    label = str(reply.get("label", "")).upper().strip()
    quote = str(reply.get("quote") or "").strip()
    conf = str(reply.get("confidence", "")).lower()
    if label not in RELATIONS:
        return "none", label or None, None, quote or None, None
    if conf not in ("high", "medium"):
        return "low_confidence", label, None, quote, None
    nq, nex = _norm(quote), _norm(excerpts)
    if len(nq) < 20 or nq not in nex:
        return "quote_not_found", label, None, quote, None
    if not (any(t in nq for t in name_terms(name_a)) and any(t in nq for t in name_terms(name_b))):
        return "quote_lacks_names", label, None, quote, None
    # which article the quote came from
    quote_url = None
    for block in excerpts.split("\n\n[Article "):
        if nq in _norm(block):
            first = block.split("\n", 1)[0]
            quote_url = first.split("] ", 1)[-1].strip()
            break
    return "promoted", label, RELATIONS[label], quote, quote_url


def collect(db, client):
    cur = db.cursor()
    cur.execute("SELECT batch_id FROM gdelt_haiku_batches WHERE status = 'in_progress' ORDER BY submitted_at")
    open_batches = [r[0] for r in cur.fetchall()]
    dbm = DBManager(DB_PATH)
    n_done = 0
    for bid in open_batches:
        b = client.messages.batches.retrieve(bid)
        if b.processing_status != "ended":
            continue
        cur.execute("SELECT pair_hash, name_a, name_b, excerpts, occurrence_count FROM gdelt_haiku_review WHERE batch_id = %s", (bid,))
        rows = {r[0]: r for r in cur.fetchall()}
        counts, final = {}, {}
        for res in client.messages.batches.results(bid):
            row = rows.get(res.custom_id)
            if row is None:
                continue
            h, a, b_, ex, n = row
            if res.result.type != "succeeded":
                cur.execute("UPDATE gdelt_haiku_review SET stage='fetched', batch_id=NULL, updated_at=now() WHERE pair_hash=%s", (h,))
                counts["retry"] = counts.get("retry", 0) + 1
                continue
            msg = res.result.message
            tool_in = next((c.input for c in msg.content if getattr(c, "type", "") == "tool_use"), None)
            text = json.dumps(tool_in) if tool_in is not None else "".join(getattr(c, "text", "") for c in msg.content)
            tin, tout = msg.usage.input_tokens, msg.usage.output_tokens
            # actual batch billing is half the standard rate
            cost = (tin * PRICE_IN_PER_TOK + tout * PRICE_OUT_PER_TOK) / 2
            reply = tool_in if isinstance(tool_in, dict) else parse_reply(text)
            verdict, label, rel, quote, qurl = judge(reply, ex, a, b_)
            counts[verdict] = counts.get(verdict, 0) + 1
            cur.execute("UPDATE gdelt_haiku_review SET stage='done', label=%s, relation_type=%s, quote=%s, quote_url=%s, "
                        "confidence=%s, verdict=%s, raw_reply=%s, input_tokens=%s, output_tokens=%s, cost_usd=coalesce(cost_usd, 0) + %s, "
                        "updated_at=now() WHERE pair_hash=%s",
                        (label, rel, quote, qurl, (reply or {}).get("confidence"), verdict, text[:2000], tin, tout, cost, h))
            if verdict == "promoted":
                evidence = json.dumps({"note": f"GDELT co-mention ({n:,} mentions) read by {MODEL}",
                                       "quote": quote, "url": qurl, "label": label})
                dbm.add_relationship(None, a, "PERSON", None, b_, "PERSON", rel, "GDELT_HAIKU", evidence)
            final.setdefault("promoted" if verdict == "promoted" else "rejected", []).append(h)
            n_done += 1
        cur.execute("UPDATE gdelt_haiku_batches SET status='ended' WHERE batch_id=%s", (bid,))
        db.commit()
        set_evidence_status(db, final)
        actual, pending = spend_state(db)
        log(f"collect: batch {bid}: {counts}; spent so far ${actual:.2f}")
    dbm.close()
    return n_done


def summary(db):
    cur = db.cursor()
    cur.execute("SELECT stage, verdict, count(*) FROM gdelt_haiku_review GROUP BY 1, 2 ORDER BY 1, 2")
    actual, pending = spend_state(db)
    log("summary: " + ", ".join(f"{s}/{v or '-'}={n:,}" for s, v, n in cur.fetchall())
        + f"; spent ${actual:.2f}, in flight (worst case) ${pending:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["select", "fetch", "submit", "collect", "run", "summary"])
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    db = connect()
    ensure_table(db)
    if args.cmd == "select":
        select(db, args.limit)
    elif args.cmd == "fetch":
        fetch(db, args.limit)
    elif args.cmd == "submit":
        submit(db, anthropic_client(), args.limit)
    elif args.cmd == "collect":
        collect(db, anthropic_client())
    elif args.cmd == "run":
        if os.name == "nt":   # a multi-hour run; keep Windows from sleeping under it
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        client = anthropic_client()
        cur = db.cursor()
        select(db, args.limit)   # idempotent: only takes still-'unreviewed' pairs
        capped = False
        while True:
            collect(db, client)
            fetched_now = fetch(db)
            cur.execute("SELECT count(*) FROM gdelt_haiku_review WHERE stage = 'fetched'")
            ready = cur.fetchone()[0]
            if not capped and (ready >= BATCH_SIZE or (fetched_now == 0 and ready)):
                capped = submit(db, client) == -1
            cur.execute("SELECT count(*) FILTER (WHERE stage IN ('selected', 'fetched', 'submitted')), "
                        "count(*) FILTER (WHERE stage = 'submitted') FROM gdelt_haiku_review")
            open_n, submitted_n = cur.fetchone()
            if open_n == 0 or (capped and submitted_n == 0):
                break
            if fetched_now == 0:
                time.sleep(120)   # only waiting on batches now
    summary(db)
    db.close()


if __name__ == "__main__":
    main()
