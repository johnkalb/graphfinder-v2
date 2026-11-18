"""Caucus membership per Congress from Wikipedia history (2026-11-18).

For each caucus article, the revision in effect in the middle of each Congress
(1 June of its second year) is parsed; people linked inside the membership
sections are resolved through Wikidata to bioguide ids (legislators YAML) and
kept only if they served in that Congress. Article histories start around
2005, so coverage is roughly the 109th Congress on.

  python congress_caucuses.py            # writes data/raw/congress/caucuses.jsonl
  python congress_caucuses.py --only "Freedom Caucus" --verbose
"""
import argparse
import collections
import json
import os
import re
import sys
import time
from datetime import date

import requests

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "raw", "congress")
OUT = os.path.join(RAW, "caucuses.jsonl")
API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "sixdegrees-congress-pilot/1.0 (research; contact via site)"}

# article title -> display name; a list article where the caucus has one
CAUCUSES = {
    "Freedom Caucus": "House Freedom Caucus",
    "Blue Dog Coalition": "Blue Dog Coalition",
    "Congressional Progressive Caucus": "Congressional Progressive Caucus",
    "New Democrat Coalition": "New Democrat Coalition",
    "Problem Solvers Caucus": "Problem Solvers Caucus",
    "Republican Study Committee": "Republican Study Committee",
    "Republican Main Street Caucus": "Republican Main Street Caucus",
    "Republican Governance Group": "Republican Governance Group (Tuesday Group)",
    "Congressional Black Caucus": "Congressional Black Caucus",
    "Congressional Hispanic Caucus": "Congressional Hispanic Caucus",
    "Congressional Hispanic Conference": "Congressional Hispanic Conference",
    "Congressional Asian Pacific American Caucus": "Congressional Asian Pacific American Caucus",
    "Congressional Western Caucus": "Congressional Western Caucus",
    "Climate Solutions Caucus": "Climate Solutions Caucus",
    "Tea Party Caucus": "Tea Party Caucus",
    "Liberty Caucus": "Liberty Caucus",
    "Congressional Pro-Life Caucus": "Congressional Pro-Life Caucus",
}
MEMBER_HDR = re.compile(r"member|roster|leadership|chair", re.I)
SKIP_HDR = re.compile(r"former|see also|reference|external|notes|history|electoral|endorse", re.I)


def get(params):
    params = {"format": "json", "formatversion": 2, "maxlag": 5, **params}
    last = None
    for i in range(8):
        try:
            r = requests.get(API, params=params, headers=UA, timeout=60)
            last = r.status_code
            if r.status_code == 200 and "error" not in r.json():
                time.sleep(0.3)              # stay polite: a few requests per second at most
                return r.json()
            wait = int(r.headers.get("Retry-After", 0) or 0)
        except requests.RequestException as e:
            last, wait = repr(e), 0
        time.sleep(max(wait, 5 * (i + 1)))
    raise RuntimeError(f"wikipedia api failed ({last}): {params}")


def revision_at(title, d):
    j = get({"action": "query", "prop": "revisions", "titles": title, "rvlimit": 1, "rvdir": "older",
             "rvstart": f"{d.isoformat()}T00:00:00Z", "rvprop": "ids|timestamp", "redirects": 1})
    pages = j.get("query", {}).get("pages", [])
    if not pages or "revisions" not in pages[0]:
        return None
    return pages[0]["revisions"][0]["revid"]


def member_links(revid):
    """Article links inside membership sections (h2/h3 headers mentioning members)."""
    j = get({"action": "parse", "oldid": revid, "prop": "text", "disablelimitreport": 1})
    html = j.get("parse", {}).get("text", "")
    parts = re.split(r"(<h[23][^>]*>.*?</h[23]>)", html, flags=re.S)
    links, keep = set(), False
    for p in parts:
        m = re.match(r"<h([23])[^>]*>(.*?)</h[23]>", p, re.S)
        if m:
            hdr = re.sub("<[^>]+>", "", m.group(2))
            if m.group(1) == "2" or MEMBER_HDR.search(hdr) or SKIP_HDR.search(hdr):
                keep = bool(MEMBER_HDR.search(hdr)) and not SKIP_HDR.search(hdr)
            continue
        if keep:
            for t in re.findall(r'href="/wiki/([^"#?]+)"', p):
                if ":" not in t:
                    links.add(requests.utils.unquote(t).replace("_", " "))
    return links


_qid_cache = {}


def titles_to_qids(titles):
    todo = [t for t in titles if t not in _qid_cache]
    for i in range(0, len(todo), 50):
        chunk = todo[i:i + 50]
        j = get({"action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "titles": "|".join(chunk),
                 "redirects": 1})
        q = j.get("query", {})
        redir = {r["from"]: r["to"] for r in q.get("redirects", [])}
        norm = {n["from"]: n["to"] for n in q.get("normalized", [])}
        found = {p["title"]: p.get("pageprops", {}).get("wikibase_item") for p in q.get("pages", [])}
        for t in chunk:
            t2 = norm.get(t, t)
            t2 = redir.get(t2, t2)
            _qid_cache[t] = found.get(t2)
    return {t: _qid_cache.get(t) for t in titles}


def load_members():
    import yaml
    by_qid, served = {}, collections.defaultdict(set)
    for fn in ("legislators-historical.yaml", "legislators-current.yaml"):
        for m in yaml.safe_load(open(os.path.join(RAW, fn), encoding="utf-8")):
            b, q = m["id"]["bioguide"], m["id"].get("wikidata")
            if q:
                by_qid[q] = b
            for t in m.get("terms", []):
                s, e = date.fromisoformat(str(t["start"])), date.fromisoformat(str(t["end"]))
                for c in range((s.year - 1789) // 2 + 1, (e.year - 1789) // 2 + 1):
                    served[b].add(c)
    return by_qid, served


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--first", type=int, default=109)
    ap.add_argument("--last", type=int, default=119)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    by_qid, served = load_members()
    rows = []
    for title, display in CAUCUSES.items():
        if a.only and a.only != title:
            continue
        per = {}
        for c in range(a.first, a.last + 1):
            y = 1789 + 2 * (c - 1) + 1
            d = min(date(y, 6, 1), date.today())
            rev = revision_at(title, d)
            if not rev:
                continue
            links = member_links(rev)
            qids = titles_to_qids(sorted(links))
            bs = {by_qid[q] for q in qids.values() if q in by_qid}
            bs = {b for b in bs if c in served[b]}
            per[c] = bs
            for b in sorted(bs):
                rows.append({"caucus": display, "bioguide": b, "congress": c})
        print(f"{display}: " + ", ".join(f"{c}:{len(v)}" for c, v in sorted(per.items())), flush=True)
    if a.only and not a.verbose:
        return
    if not a.only:
        with open(OUT, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"wrote {len(rows):,} rows to {OUT}")


if __name__ == "__main__":
    main()
