"""Angel and venture investors from funding press releases (2026-11-13).

The graph knows who runs funds (SEC Form D) but not who puts money into
startups. Funding announcements name them ("led by X Ventures, with
participation from angel investors Jane Doe and ..."). This reads PR Newswire
releases -- its monthly sitemaps go back to April 2014 and its robots.txt
allows crawling -- picks the funding announcements by URL, fetches them, and
has Claude Haiku list the round's investors with a verbatim quote for each.

Rows written (source_data PRNEWSWIRE_FUNDING, relation INVESTED_IN, which
webapp/relation_categories maps to FINANCIAL):
    PERSON -INVESTED_IN-> COMPANY   individuals investing personally (angels)
    ORG    -INVESTED_IN-> COMPANY   funds and firms
Evidence: url, title, month, round, amount, lead/participant, angel flag,
the quote, and for firms the partner named with them.

A link is kept only if its quote is copied exactly from the release and names
the investor; the company must be named in the release too.

TechCrunch is deliberately not used: its robots.txt disallows AI crawlers.

State: Postgres tables funding_news (one row per release URL, stage
found -> fetched/skip/gone -> submitted -> done) and funding_news_batches.

Usage:
    python angel_news_extract.py discover --months 3      # newest N monthly sitemaps
    python angel_news_extract.py discover --all
    python angel_news_extract.py fetch --limit 200
    python angel_news_extract.py extract --sync --limit 50   # pilot: direct calls
    python angel_news_extract.py run                          # fetch + batch extract until done
    python angel_news_extract.py summary
"""
import argparse
import gzip
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, r"C:\Users\johnk\AppData\Local\hermes\scripts")
from pg_secret import load_secret  # noqa: E402
if not os.environ.get("DATABASE_URL"):
    os.environ["DATABASE_URL"] = load_secret("DATABASE_URL")
import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import requests  # noqa: E402
from src.data.db_manager import DBManager  # noqa: E402
from src.config import DB_PATH  # noqa: E402

SOURCE = "PRNEWSWIRE_FUNDING"
MODEL = "claude-haiku-5-5"
SPEND_CAP_USD = float(os.environ.get("ANGEL_NEWS_CAP_USD", 50))
PRICE_IN_PER_TOK = 0.10 / 1_000_000      # standard rate; batches bill half
PRICE_OUT_PER_TOK = 0.50 / 1_000_000
MAX_OUT_TOKENS = 2000
BATCH_SIZE = 200
TEXT_CHARS = 15000
FETCH_THREADS = 2
FETCH_SPACING_SEC = 1.0                   # per thread: about 2 pages a second in all
UA = {"User-Agent": "Mozilla/5.0 (sixdegrees research)"}
SITEMAP_INDEX = "https://www.prnewswire.com/sitemap-gz.xml"
LOG_FILE = os.path.join(HERE, "logs", "angel_news_extract.log")

# Funding announcements by URL slug. Hyphen-anchored so "los-angeles" and
# "lemon-seed-marketing" don't match; false positives are cheap (the text
# filter and the model drop them).
SLUG_RE = re.compile(
    r"/news-releases/[^/]*?(?:^|-)(?:"
    r"pre-seed|seed-(?:round|funding|financing|investment|extension|raise)|"
    r"series-[a-h]-(?:round|funding|financing|investment|extension|raise|fundraise|preferred-financing)|"
    r"raises-|raised-|closes-[\w-]*?(?:round|funding|financing)|"
    r"funding-round|angel-(?:round|investors?|funding|investment|investors-)|"
    r"venture-(?:funding|financing|round|capital-funding)|"
    r"(?:million|billion|m)-(?:round|funding|financing|investment|seed))", re.I)
NOT_FUNDING_SLUG = re.compile(r"raises-(?:the-|awareness|concerns|questions|prices?|rates?|dividend|guidance|outlook|its-)|"
                              r"preferred-stock|dividend|debt-financing|notes-offering|ipo", re.I)
# The release itself must name investors somehow.
INVESTOR_TEXT = re.compile(r"\b(?:led by|co-led|participation (?:from|of)|investors? (?:includ\w*|such as)|"
                           r"angel|backed by|joined by|with support from|existing investors|"
                           r"(?:investment|funding|financing|capital|round) from)\b", re.I)
ROUND_TEXT = re.compile(r"\b(?:round|funding|financing|raised|raises|investment)\b", re.I)

SYSTEM_PROMPT = """You read a press release and list the investors in the funding round it announces.

Record the result with the record_round tool.

- is_funding_round: true only if the release announces that a company raised money from investors (seed, angel, Series A-H, venture, growth or strategic equity round). False for loans, debt, grants, IPOs, acquisitions, fund closes of a VC firm itself, charity fundraising, or anything else.
- company: the company that raised the money, as named in the release.
- round: e.g. "Seed", "Pre-seed", "Series A", "Angel", "Strategic"; empty if not stated.
- amount_usd: the round size in US dollars if stated in dollars, else null.
- investors: every investor in THIS round named in the release.
  - kind "person" only for an individual investing personally: an angel investor, a founder or executive of another company investing their own money, an athlete or celebrity investor. Set angel=true for these.
  - kind "firm" for funds, venture firms, corporations, family offices, angel groups and syndicates. If a partner of the firm is named with it ("led by Jane Doe of Acme Ventures"), put the firm as the investor and the partner's name in representative. Do NOT list that partner as a person.
  - role "lead" if the release says the investor led or co-led the round, else "participant".
  - affiliation: for a person, what the release says they are known for (e.g. "co-founder of Stripe"); else empty.
  - quote: one sentence copied exactly from the release that names this investor. Copy it character for character.
- Do not list the company's own founders or executives unless the release says they invested. Do not list advisors, banks, law firms or placement agents.
- Use only what the release says. If no investors are named, return an empty list.

Never infer names from your own knowledge."""

ROUND_TOOL = {
    "name": "record_round",
    "description": "Record the funding round and its named investors.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_funding_round": {"type": "boolean"},
            "company": {"type": "string"},
            "round": {"type": "string"},
            "amount_usd": {"type": ["number", "null"]},
            "investors": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": ["person", "firm"]},
                    "role": {"type": "string", "enum": ["lead", "participant"]},
                    "angel": {"type": "boolean"},
                    "affiliation": {"type": "string"},
                    "representative": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["name", "kind", "role", "angel", "quote"],
            }},
        },
        "required": ["is_funding_round", "company", "round", "amount_usd", "investors"],
    },
}


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def ensure_tables(db):
    cur = db.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS funding_news (
        url text PRIMARY KEY, month text, stage text NOT NULL DEFAULT 'found',
        title text, body text, batch_id text, result jsonb, n_links int,
        input_tokens int, output_tokens int, cost_usd double precision,
        found_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now())""")
    cur.execute("CREATE INDEX IF NOT EXISTS funding_news_stage ON funding_news(stage)")
    cur.execute("""CREATE TABLE IF NOT EXISTS funding_news_batches (
        batch_id text PRIMARY KEY, n_requests int, worst_case_usd double precision,
        status text DEFAULT 'in_progress', submitted_at timestamptz DEFAULT now())""")
    db.commit()


# --- discover ------------------------------------------------------------------

def _get_xml(url):
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=120)
            if r.status_code == 200:
                raw = r.content
                return (gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw).decode("utf-8", "replace")
        except requests.RequestException:
            pass
        time.sleep(10 * (attempt + 1))
    return None


def monthly_sitemaps():
    """[(month 'YYYY-MM', url)] newest first."""
    text = _get_xml(SITEMAP_INDEX) or ""
    out = []
    for u in re.findall(r"<loc>([^<]+)</loc>", text):
        m = re.search(r"Sitemap_Index_([A-Za-z]{3})_(\d{4})", u)
        if m:
            out.append((datetime.strptime(f"{m.group(1)} {m.group(2)}", "%b %Y").strftime("%Y-%m"), u))
    return sorted(out, reverse=True)


def is_funding_url(u):
    return bool(SLUG_RE.search(u)) and not NOT_FUNDING_SLUG.search(u)


def discover(db, months=None):
    maps = monthly_sitemaps()
    if months:
        maps = maps[:months]
    cur = db.cursor()
    for month, url in maps:
        text = _get_xml(url)
        if text is None:
            log(f"discover: {month}: sitemap unavailable")
            continue
        locs = re.findall(r"<loc>([^<]+)</loc>", text)
        subs = [u for u in locs if u.endswith((".xml", ".xml.gz", ".gz"))]
        for s in subs:                      # an index of sub-sitemaps, if the month is split
            t = _get_xml(s)
            if t:
                locs += re.findall(r"<loc>([^<]+)</loc>", t)
        hits = sorted({u for u in locs if "/news-releases/" in u and is_funding_url(u)})
        if hits:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO funding_news (url, month) VALUES %s ON CONFLICT DO NOTHING",
                [(u, month) for u in hits])
        db.commit()
        log(f"discover: {month}: {len(locs):,} releases, {len(hits)} funding-like")


# --- fetch ---------------------------------------------------------------------

_extract_lock = threading.Lock()


def fetch_one(url):
    """-> (stage, title, body)."""
    import trafilatura
    try:
        r = requests.get(url, headers=UA, timeout=30)
    except requests.RequestException:
        return "found", None, None          # transient: try again next round
    if r.status_code in (404, 410):
        return "gone", None, None
    if r.status_code != 200:
        return "found", None, None
    # requests falls back to ISO-8859-1 for these UTF-8 pages (mangled Greek letters)
    html = r.content.decode("utf-8", errors="replace")
    with _extract_lock:
        body = trafilatura.extract(html) or ""
        meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta else "") or ""
    if not body:
        return "gone", title, None
    if not (INVESTOR_TEXT.search(body) and ROUND_TEXT.search(body)):
        return "skip", title, None
    return "fetched", title, body[:TEXT_CHARS]


def fetch(db, limit=500):
    cur = db.cursor()
    cur.execute("SELECT url FROM funding_news WHERE stage = 'found' ORDER BY month DESC, url LIMIT %s", (limit,))
    urls = [r[0] for r in cur.fetchall()]
    if not urls:
        return 0
    counts = {}

    def work(chunk):
        out = []
        for u in chunk:
            t0 = time.time()
            out.append((u, *fetch_one(u)))
            time.sleep(max(0.0, FETCH_SPACING_SEC - (time.time() - t0)))
        return out

    chunks = [urls[i::FETCH_THREADS] for i in range(FETCH_THREADS)]
    with ThreadPoolExecutor(FETCH_THREADS) as ex:
        results = [row for part in ex.map(work, chunks) for row in part]
    for u, stage, title, body in results:
        counts[stage] = counts.get(stage, 0) + 1
        cur.execute("UPDATE funding_news SET stage=%s, title=%s, body=%s, updated_at=now() WHERE url=%s",
                    (stage, title, body, u))
    db.commit()
    log(f"fetch: {len(urls)} releases: {counts}")
    return len(urls)


# --- extract -------------------------------------------------------------------

def anthropic_client():
    import anthropic
    return anthropic.Anthropic(api_key=load_secret("ANTHROPIC_API_KEY"))


def request_params(title, body):
    return {"model": MODEL, "max_tokens": MAX_OUT_TOKENS, "system": SYSTEM_PROMPT,
            # Haiku 5.5 thinks by default; this is plain extraction
            "thinking": {"type": "disabled"},
            "tools": [ROUND_TOOL], "tool_choice": {"type": "tool", "name": "record_round"},
            "messages": [{"role": "user", "content": f"Title: {title or ''}\n\n{body}"}]}


def _norm(s):
    s = (s or "").replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


CORP_WORDS = re.compile(r"\b(LLC|L\.?P\.?|Inc|Corp|Ventures?|Capital|Partners|Fund|Holdings|Group|Investments?|"
                        r"Management|Labs|Foundation|Trust|Bank|Angels|Network|Syndicate|Family Office)\b", re.I)


def clean_person(name):
    name = re.sub(r"\s+", " ", (name or "").strip(" ,.;"))
    parts = name.split()
    if len(parts) < 2 or len(parts) > 5 or CORP_WORDS.search(name) or any(ch.isdigit() for ch in name):
        return None
    return name


def _key(s):
    return re.sub(r"[^0-9a-z]", "", _norm(s))


def sentences(text):
    out = []
    for para in (text or "").split("\n"):
        out += [x.strip() for x in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", para) if x.strip()]
    return out


def find_quote(sents, name, model_quote):
    """-> (quote, context): a sentence copied from the release that names the
    investor -- the model's quote if it is verbatim, else the release sentence
    naming them that best reads as investment news (Haiku condenses sentences
    rather than copying them, 2026-11-13 pilot). When that sentence is a bare
    list entry ("- Ben Lurie"), context is the nearest sentence above it that
    talks about investors (the list's heading). (None, None) if the release
    never names them."""
    kname = _key(re.sub(r"\s*\([^)]*\)", "", name))
    if not kname:
        return None, None
    mq = _key(model_quote)
    if mq and kname in mq:
        for x in sents:
            if mq in _key(x):
                return x[:600], None
    hits = [i for i, x in enumerate(sents) if kname in _key(x)]
    if not hits:
        return None, None
    hits.sort(key=lambda i: (not INVESTOR_TEXT.search(sents[i]), not ROUND_TEXT.search(sents[i]), len(sents[i])))
    i = hits[0]
    quote, context = sents[i][:600], None
    if not INVESTOR_TEXT.search(quote) and not ROUND_TEXT.search(quote):
        context = next((sents[j][:400] for j in range(i - 1, max(-1, i - 25), -1)
                        if INVESTOR_TEXT.search(sents[j]) or ROUND_TEXT.search(sents[j])), None)
    return quote, context


def validate(reply, body, title=""):
    """-> (company, [kept investor dicts], counts)."""
    counts = {}
    if not isinstance(reply, dict) or not reply.get("is_funding_round"):
        return None, [], {"not_round": 1}
    full = f"{title or ''}\n{body}"
    sents = sentences(full)
    company = (reply.get("company") or "").strip()
    if not company or _key(company) not in _key(full):
        return None, [], {"company_not_in_text": 1}
    kept, seen = [], set()
    investors = reply.get("investors") or []
    if isinstance(investors, str):        # the tool has no strict schema: now and then a JSON string
        try:
            investors = json.loads(investors)
        except ValueError:
            investors = []
    for inv in investors if isinstance(investors, list) else []:
        if not isinstance(inv, dict):      # or a bare name instead of an object (2026-11-13 run)
            counts["malformed"] = counts.get("malformed", 0) + 1
            continue
        name = (inv.get("name") or "").strip()
        kind = inv.get("kind")
        if kind == "person":
            # a person not presented as investing personally is usually a
            # fund partner speaking for the firm (pilot: Lu Zhang of Fusion Fund)
            if not inv.get("angel"):
                counts["person_not_angel"] = counts.get("person_not_angel", 0) + 1
                continue
            name = clean_person(name)
            if not name:
                counts["bad_person_name"] = counts.get("bad_person_name", 0) + 1
                continue
            key_term = name.split()[-1]
        else:
            key_term = re.split(r"\s+", re.sub(r"[^\w\s&'-]", " ", name).strip())[0] if name else ""
        quote, context = find_quote(sents, name, inv.get("quote")) if name else (None, None)
        if not quote or _key(key_term) not in _key(quote):
            counts["name_not_in_text"] = counts.get("name_not_in_text", 0) + 1
            continue
        if _norm(name) == _norm(company) or (kind, _norm(name)) in seen:
            continue
        seen.add((kind, _norm(name)))
        kept.append({**inv, "name": name, "quote": quote, "context": context})
        counts[f"kept_{kind}"] = counts.get(f"kept_{kind}", 0) + 1
    return company, kept, counts


def write_links(dbm, url, title, month, reply, company, kept):
    for inv in kept:
        ev = {"source": SOURCE, "url": url, "title": title, "month": month,
              "round": reply.get("round") or None, "amount_usd": reply.get("amount_usd"),
              "role": inv.get("role"), "angel": bool(inv.get("angel")) and inv.get("kind") == "person",
              "quote": inv.get("quote"), "model": MODEL}
        if inv.get("affiliation"):
            ev["affiliation"] = inv["affiliation"]
        if inv.get("representative"):
            ev["representative"] = inv["representative"]
        if inv.get("context"):
            ev["context"] = inv["context"]
        dbm.add_relationship(None, inv["name"], "PERSON" if inv["kind"] == "person" else "ORG",
                             None, company, "COMPANY", "INVESTED_IN", SOURCE, json.dumps(ev))


def tool_input(msg):
    return next((c.input for c in msg.content if getattr(c, "type", "") == "tool_use"), None)


def finish(cur, dbm, url, title, month, body, msg, cost):
    reply = tool_input(msg)
    try:
        company, kept, counts = validate(reply, body, title)
        if kept:
            write_links(dbm, url, title, month, reply, company, kept)
    except Exception as e:                 # one odd reply must not stop a multi-hour run
        log(f"finish: {url}: {type(e).__name__}: {e}")
        kept, counts = [], {"error": 1}
    cur.execute("UPDATE funding_news SET stage='done', result=%s, n_links=%s, input_tokens=%s, output_tokens=%s, "
                "cost_usd=coalesce(cost_usd,0)+%s, updated_at=now() WHERE url=%s",
                (json.dumps({"reply": reply, "counts": counts}), len(kept),
                 msg.usage.input_tokens, msg.usage.output_tokens, cost, url))
    return counts


def spend_state(db):
    cur = db.cursor()
    cur.execute("SELECT coalesce(sum(cost_usd),0) FROM funding_news")
    actual = float(cur.fetchone()[0])
    cur.execute("SELECT coalesce(sum(worst_case_usd),0) FROM funding_news_batches WHERE status='in_progress'")
    return actual, float(cur.fetchone()[0])


def worst_case(title, body):
    est_in = int((len(SYSTEM_PROMPT) + len(title or "") + len(body)) / 3.5 * 1.25) + 600
    return est_in * PRICE_IN_PER_TOK + MAX_OUT_TOKENS * PRICE_OUT_PER_TOK


def extract_sync(db, client, limit=50):
    """Pilot path: direct calls, standard rate."""
    cur = db.cursor()
    cur.execute("SELECT url, title, month, body FROM funding_news WHERE stage='fetched' ORDER BY month DESC, url LIMIT %s",
                (limit,))
    rows = cur.fetchall()
    dbm = DBManager(DB_PATH)
    total = {}
    for url, title, month, body in rows:
        actual, pending = spend_state(db)
        if actual + pending + worst_case(title, body) > SPEND_CAP_USD:
            log(f"extract: STOP at the ${SPEND_CAP_USD:.0f} cap")
            break
        msg = client.messages.create(**request_params(title, body))
        cost = msg.usage.input_tokens * PRICE_IN_PER_TOK + msg.usage.output_tokens * PRICE_OUT_PER_TOK
        for k, v in finish(cur, dbm, url, title, month, body, msg, cost).items():
            total[k] = total.get(k, 0) + v
        db.commit()
    dbm.close()
    log(f"extract (sync): {len(rows)} releases: {total}")
    return len(rows)


def submit(db, client):
    cur = db.cursor()
    cur.execute("SELECT url, title, body FROM funding_news WHERE stage='fetched' ORDER BY month DESC, url LIMIT %s",
                (BATCH_SIZE,))
    rows = cur.fetchall()
    if not rows:
        return 0
    worst = sum(worst_case(t, b) for _, t, b in rows)
    actual, pending = spend_state(db)
    if actual + pending + worst > SPEND_CAP_USD:
        log(f"submit: STOP -- spent ${actual:.2f} + in flight ${pending:.2f} + ${worst:.2f} > ${SPEND_CAP_USD:.0f} cap")
        return -1
    ids = {f"r{i}": u for i, (u, _, _) in enumerate(rows)}
    reqs = [{"custom_id": cid, "params": request_params(t, b)} for cid, (u, t, b) in zip(ids, rows)]
    import anthropic
    for attempt in range(5):
        try:
            batch = client.messages.batches.create(requests=reqs)
            break
        except anthropic.APIConnectionError as e:
            log(f"submit: connection error ({e}); retry {attempt + 1}/5")
            time.sleep(30 * (attempt + 1))
    else:
        return 0
    cur.execute("INSERT INTO funding_news_batches (batch_id, n_requests, worst_case_usd) VALUES (%s,%s,%s)",
                (batch.id, len(reqs), worst))
    for cid, u in ids.items():
        cur.execute("UPDATE funding_news SET stage='submitted', batch_id=%s, result=%s, updated_at=now() WHERE url=%s",
                    (batch.id, json.dumps({"custom_id": cid}), u))
    db.commit()
    log(f"submit: batch {batch.id}: {len(reqs)} releases (worst case ${worst:.2f})")
    return len(reqs)


def collect(db, client):
    cur = db.cursor()
    cur.execute("SELECT batch_id FROM funding_news_batches WHERE status='in_progress' ORDER BY submitted_at")
    done = 0
    dbm = DBManager(DB_PATH)
    for (bid,) in cur.fetchall():
        b = client.messages.batches.retrieve(bid)
        if b.processing_status != "ended":
            continue
        c2 = db.cursor()
        c2.execute("SELECT url, title, month, body, result->>'custom_id' FROM funding_news WHERE batch_id=%s", (bid,))
        rows = {r[4]: r for r in c2.fetchall()}
        results = list(client.messages.batches.results(bid))   # read all before writing
        total = {}
        for res in results:
            row = rows.get(res.custom_id)
            if not row:
                continue
            url, title, month, body, _ = row
            if res.result.type != "succeeded":
                c2.execute("UPDATE funding_news SET stage='fetched', batch_id=NULL WHERE url=%s", (url,))
                continue
            msg = res.result.message
            cost = (msg.usage.input_tokens * PRICE_IN_PER_TOK + msg.usage.output_tokens * PRICE_OUT_PER_TOK) / 2
            for k, v in finish(c2, dbm, url, title, month, body, msg, cost).items():
                total[k] = total.get(k, 0) + v
            done += 1
        c2.execute("UPDATE funding_news_batches SET status='ended' WHERE batch_id=%s", (bid,))
        db.commit()
        actual, _ = spend_state(db)
        log(f"collect: batch {bid}: {total}; spent ${actual:.2f}")
    dbm.close()
    return done


def summary(db):
    cur = db.cursor()
    cur.execute("SELECT stage, count(*) FROM funding_news GROUP BY 1 ORDER BY 2 DESC")
    print("stages:", cur.fetchall())
    cur.execute("SELECT source_type, count(*), count(DISTINCT source_name) FROM relationships "
                "WHERE source_data=%s GROUP BY 1", (SOURCE,))
    print("links (type, rows, distinct investors):", cur.fetchall())
    cur.execute("SELECT count(DISTINCT source_name) FROM relationships WHERE source_data=%s "
                "AND source_type='PERSON' AND evidence::jsonb->>'angel'='true'", (SOURCE,))
    print("distinct angels:", cur.fetchone()[0])
    print("spent: $%.2f (in flight worst case $%.2f)" % spend_state(db))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["discover", "fetch", "extract", "run", "summary"])
    ap.add_argument("--months", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--sync", action="store_true")
    a = ap.parse_args()
    db = connect()
    ensure_tables(db)
    if a.cmd == "discover":
        discover(db, None if a.all else (a.months or 1))
    elif a.cmd == "fetch":
        fetch(db, a.limit)
    elif a.cmd == "extract":
        client = anthropic_client()
        if a.sync:
            extract_sync(db, client, a.limit)
        else:
            collect(db, client)
            submit(db, client)
    elif a.cmd == "run":
        client = anthropic_client()
        while True:
            fetched = fetch(db, 400)
            collect(db, client)
            stop = False
            while True:
                n = submit(db, client)
                if n == -1:
                    stop = True
                if n <= 0:
                    break
            cur = db.cursor()
            cur.execute("SELECT count(*) FROM funding_news WHERE stage IN ('found','fetched','submitted')")
            left = cur.fetchone()[0]
            if left == 0 or (stop and not fetched):
                collect(db, client)
                break
            if not fetched:
                time.sleep(120)        # only waiting on batches
        summary(db)
    else:
        summary(db)


if __name__ == "__main__":
    main()
