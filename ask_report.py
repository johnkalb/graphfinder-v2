"""What beta testers asked in plain English, and what it cost (2026-10-20).

Reads /api/internal/user-links/ask-log (USER_LINKS_SYNC_TOKEN header, the
same Cloudflare Bypass path as check_latency.py), which returns the ask_log
rows the web app writes for every question.

  python ask_report.py               # last 7 days
  python ask_report.py --days 30
  python ask_report.py --full        # every question with its answer and lookups
"""
import argparse
import collections
import os
import statistics
import sys

import requests

HERMES_SCRIPTS = r"C:\Users\johnk\AppData\Local\hermes\scripts"
sys.path.insert(0, HERMES_SCRIPTS)
import pg_secret  # noqa: E402

URL = os.environ.get("ASK_LOG_URL", "https://sixdegrees.net/api/internal/user-links/ask-log")


def fetch(days):
    r = requests.get(URL, params={"days": days},
                     headers={"X-Sync-Token": pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"),
                              "User-Agent": "sixdegrees-ask-report"}, timeout=60)
    r.raise_for_status()
    return r.json()


def short(s, n=110):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--full", action="store_true", help="print every question with its answer and lookups")
    args = ap.parse_args()
    d = fetch(args.days)
    rows = list(reversed(d["rows"]))   # oldest first
    print(f"Last {d['days']:g} days: {len(rows)} questions from {len({r['email'] for r in rows})} users; "
          f"model {d['model']}; daily limit {d['daily_limit']}/user; "
          f"this month ${d['month_cost_usd']:.4f} of the ${d['monthly_cap_usd']:g} cap")
    if not rows:
        return
    by_status = collections.Counter(r["status"] for r in rows)
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    secs = [r["ms"] / 1000 for r in rows if r["ms"]]
    print("Status: " + ", ".join(f"{k} {v}" for k, v in by_status.most_common()))
    if costs:
        print(f"Cost: ${sum(costs):.4f} total, ${statistics.mean(costs):.5f} average, ${max(costs):.5f} max per question")
    if secs:
        print(f"Time: {statistics.median(secs):.1f} s median, {max(secs):.1f} s max")
    print(f"Tokens: {sum(r['input_tokens'] or 0 for r in rows):,} in, {sum(r['output_tokens'] or 0 for r in rows):,} out; "
          f"steps {statistics.mean([r['steps'] or 0 for r in rows]):.1f} average")
    fb = collections.Counter(r["feedback"] for r in rows if r["feedback"])
    print(f"Feedback: {fb.get(1, 0)} helpful, {fb.get(-1, 0)} not helpful")
    tools = collections.Counter(t["tool"] for r in rows for t in (r["tools"] or []))
    errors = collections.Counter(t["tool"] for r in rows for t in (r["tools"] or []) if t.get("error"))
    print("Lookups: " + ", ".join(f"{k} {v}" + (f" ({errors[k]} failed)" if errors[k] else "")
                                  for k, v in tools.most_common()))
    flagged = [r for r in rows if (r["flags"] or {}).get("unverified_numbers")]
    if flagged:
        print(f"Answers with a number not in the records: {len(flagged)}")

    una = [r for r in rows if r["status"] == "unanswerable"]
    if una:
        print(f"\nCouldn't answer ({len(una)}):")
        for r in una:
            print(f"  {r['asked_at'][:16]}  {short(r['question'])}\n      missing: {short(r['reason'], 100)}")
    bad = [r for r in rows if r["feedback"] == -1 or r["status"] == "error" or (r["flags"] or {}).get("unverified_numbers")]
    if bad:
        print(f"\nNot helpful, failed, or flagged ({len(bad)}):")
        for r in bad:
            why = "; ".join(x for x in [r["status"] if r["status"] == "error" else "",
                                        f"note: {r['feedback_note']}" if r["feedback_note"] else "",
                                        f"numbers: {r['flags']['unverified_numbers']}" if (r["flags"] or {}).get("unverified_numbers") else "",
                                        (r["flags"] or {}).get("error", "")] if x)
            print(f"  #{r['id']} {short(r['question'])}" + (f"\n      {short(why, 150)}" if why else ""))
    if args.full:
        print("\nAll questions:")
        for r in rows:
            print(f"\n#{r['id']} {r['asked_at'][:16]} [{r['status']}] ${r['cost_usd'] or 0:.5f} {r['ms'] / 1000:.1f}s  {r['question']}")
            for t in r["tools"] or []:
                print(f"    {t['tool']}({', '.join(f'{k}={v!r}' for k, v in (t['input'] or {}).items())}) "
                      f"-> {t['rows']} rows{' ERROR ' + t['error'] if t.get('error') else ''}")
            print("    " + short(r["answer"], 400))


if __name__ == "__main__":
    main()
