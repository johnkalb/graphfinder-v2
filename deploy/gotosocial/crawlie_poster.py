#!/usr/bin/env python3
"""Crawlie poster: publish approved sixdegrees crawlie facts as @crawlie@sixdegrees.net.

Runs on optiplex from cron, in ~/gotosocial/poster/:

  crawlie_poster.py propose   # once a morning: send up to MAX_PER_DAY never-posted
                              # facts to the operator on Telegram, numbered
  crawlie_poster.py tick      # every few minutes: read the operator's reply
                              # ("all" / "1 3" / "none"), then post approved facts
                              # at most one per POST_SPACING
  crawlie_poster.py status    # print the state (no network)

Inputs:
  crawlie_facts.json.gz  copied here by rebuild_and_deploy.py after each nightly
                         build; only its "publishable" list is used (verified
                         public figures only -- see build_crawlie_facts.py)
  .env (0600)            CRAWLIE_TELEGRAM_BOT_TOKEN, CRAWLIE_TELEGRAM_CHAT_ID --
                         a bot of its own: Hermes already long-polls the pipeline
                         alert bot, and two getUpdates readers steal each other's
                         messages
  ../crawlie_credentials GoToSocial access_token for @crawlie

State: state.json -- posted fact keys (each fact posts at most once, ever), the
pending proposal, the approved queue, and the Telegram update offset.
Set CRAWLIE_DRY_RUN=1 to print instead of sending anything.
"""
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
FACTS = os.path.join(HERE, "crawlie_facts.json.gz")
STATE = os.path.join(HERE, "state.json")
GTS = "https://social.sixdegrees.net"
MAX_PER_DAY = 3
POST_SPACING = 2 * 3600          # seconds between posts
PROPOSAL_TTL = 20 * 3600         # an unanswered proposal lapses before the next morning's
DRY_RUN = os.environ.get("CRAWLIE_DRY_RUN") == "1"

HASHTAGS = {
    "group_vs_group": "#CorporateGovernance #Boards #NetworkScience",
    "who_you_know": "#PageRank #NetworkScience #Politics",
    "top_pagerank": "#PageRank #NetworkScience",
    "top_degree": "#NetworkScience #Politics",
    "top_degree_public": "#NetworkScience #Politics",
    "bridge": "#NetworkScience #Power",
}
# Proposal order: rotate through categories so a day isn't three of one kind.
CATEGORY_ORDER = ["group_vs_group", "who_you_know", "bridge", "top_degree_public", "top_pagerank", "top_degree"]


def load_env():
    env = {}
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v.strip().strip('"')
    creds = os.path.join(HERE, "..", "crawlie_credentials")
    for line in open(creds, encoding="utf-8"):
        if line.startswith("access_token="):
            env["GTS_TOKEN"] = line.strip().split("=", 1)[1]
    return env


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"posted": {}, "proposal": None, "queue": [], "tg_offset": 0, "last_post_at": 0}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE)


def log(msg):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- Telegram ------------------------------------------------------------------

def tg(env, method, **params):
    if DRY_RUN and method == "sendMessage":
        log(f"[dry-run] telegram: {params.get('text')}")
        return {"ok": True, "result": {"message_id": 0}}
    r = requests.post(f"https://api.telegram.org/bot{env['CRAWLIE_TELEGRAM_BOT_TOKEN']}/{method}",
                      json=params, timeout=40)
    return r.json()


def notify(env, text):
    tg(env, "sendMessage", chat_id=env.get("CRAWLIE_TELEGRAM_CHAT_ID"), text=text,
       disable_web_page_preview=True)


def parse_reply(text, n):
    """'all'/'yes' -> every index; 'none'/'no'/'skip' -> []; '1 3' / '1,3' -> those.
    Returns None for anything unrecognized."""
    t = text.strip().lower()
    if t in ("all", "yes", "y", "ok", "approve"):
        return list(range(n))
    if t in ("none", "no", "n", "skip"):
        return []
    picks = []
    for tok in t.replace(",", " ").split():
        if not tok.isdigit() or not 1 <= int(tok) <= n:
            return None
        if int(tok) - 1 not in picks:
            picks.append(int(tok) - 1)
    return picks or None


# --- posting -------------------------------------------------------------------

def post_text(fact):
    tags = HASHTAGS.get(fact["category"], "#NetworkScience")
    return f"{fact['text']}\n\n{tags} #sixdegrees"


def publish(env, fact):
    if DRY_RUN:
        log(f"[dry-run] would post: {post_text(fact)!r}")
        return "dry-run"
    r = requests.post(f"{GTS}/api/v1/statuses",
                      headers={"Authorization": f"Bearer {env['GTS_TOKEN']}",
                               "Idempotency-Key": fact["key"][:200]},
                      data={"status": post_text(fact), "visibility": "public", "language": "en"},
                      timeout=60)
    r.raise_for_status()
    return r.json().get("url")


# --- commands ------------------------------------------------------------------

def pick_candidates(facts, posted):
    fresh = [f for f in facts if f["key"] not in posted]
    by_cat = {}
    for f in fresh:
        by_cat.setdefault(f["category"], []).append(f)
    order = CATEGORY_ORDER + sorted(c for c in by_cat if c not in CATEGORY_ORDER)
    picks = []
    while len(picks) < MAX_PER_DAY and any(by_cat.get(c) for c in order):
        for c in order:
            if by_cat.get(c) and len(picks) < MAX_PER_DAY:
                picks.append(by_cat[c].pop(0))
    return picks


def cmd_propose(env, state):
    now = time.time()
    p = state.get("proposal")
    if p and now - p["sent_at"] < PROPOSAL_TTL:
        log("previous proposal still awaiting a reply; not sending another")
        return
    with gzip.open(FACTS, "rt", encoding="utf-8") as f:
        data = json.load(f)
    queued = {f["key"] for f in state["queue"]}
    candidates = pick_candidates(data.get("publishable", []), set(state["posted"]) | queued)
    if not candidates:
        log("no never-posted publishable facts; nothing to propose")
        state["proposal"] = None
        return
    lines = [f"Crawlie candidates for today (facts built {data.get('generated_at', '?')[:10]}):", ""]
    for i, f in enumerate(candidates, 1):
        lines.append(f"{i}. {f['text']}")
        lines.append("")
    lines.append("Reply: all · none · or numbers like 1 3")
    notify(env, "\n".join(lines))
    state["proposal"] = {"sent_at": now, "facts": candidates}
    log(f"proposed {len(candidates)} fact(s)")


def read_replies(env, state):
    """Apply the operator's reply to the pending proposal. Only messages from
    the configured chat count; everything else is ignored (and consumed)."""
    res = tg(env, "getUpdates", offset=state["tg_offset"], timeout=0,
             allowed_updates=["message"]) if not DRY_RUN else {"ok": True, "result": []}
    if not res.get("ok"):
        log(f"telegram getUpdates failed: {res.get('description')}")
        return
    for upd in res["result"]:
        state["tg_offset"] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(env["CRAWLIE_TELEGRAM_CHAT_ID"]):
            continue
        text = msg.get("text", "")
        p = state.get("proposal")
        if text.strip().lower() in ("/status", "status"):
            notify(env, f"Posted so far: {len(state['posted'])}. Queued: {len(state['queue'])}. "
                        f"Pending proposal: {'yes' if p else 'no'}.")
            continue
        if not p:
            notify(env, "Nothing is waiting for approval right now.")
            continue
        picks = parse_reply(text, len(p["facts"]))
        if picks is None:
            notify(env, f"Didn't understand that. Reply all, none, or numbers 1-{len(p['facts'])}.")
            continue
        chosen = [p["facts"][i] for i in picks]
        state["queue"].extend(chosen)
        state["proposal"] = None
        if chosen:
            notify(env, f"Approved {len(chosen)}. Posting one every {POST_SPACING // 3600}h, first one shortly.")
        else:
            notify(env, "Skipped today's candidates. They'll be offered again on a later day.")
        log(f"reply {text!r} -> queued {len(chosen)}")


def cmd_tick(env, state):
    read_replies(env, state)
    if state["queue"] and time.time() - state.get("last_post_at", 0) >= POST_SPACING:
        fact = state["queue"][0]
        try:
            url = publish(env, fact)
        except Exception as e:
            log(f"post failed, will retry next tick: {e}")
            return
        state["queue"].pop(0)
        state["posted"][fact["key"]] = {"at": datetime.now(timezone.utc).isoformat(), "url": url}
        state["last_post_at"] = time.time()
        log(f"posted {fact['key']} -> {url}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    state = load_state()
    if cmd == "status":
        print(json.dumps({"posted": len(state["posted"]), "queue": len(state["queue"]),
                          "proposal": bool(state.get("proposal"))}, indent=1))
        return
    env = load_env()
    missing = [k for k in ("CRAWLIE_TELEGRAM_BOT_TOKEN", "CRAWLIE_TELEGRAM_CHAT_ID", "GTS_TOKEN")
               if not env.get(k) and not (DRY_RUN and k.startswith("CRAWLIE_TELEGRAM"))]
    if missing:
        log(f"missing config: {', '.join(missing)}")
        sys.exit(1)
    if cmd == "propose":
        cmd_propose(env, state)
    elif cmd == "tick":
        cmd_tick(env, state)
    else:
        sys.exit(f"unknown command {cmd!r}")
    save_state(state)


if __name__ == "__main__":
    main()
