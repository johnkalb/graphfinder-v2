"""Daily Telegram report of who used sixdegrees.net (2026-10-21).

Reads /api/internal/user-links/visits (USER_LINKS_SYNC_TOKEN header, the same
Cloudflare Bypass path as check_latency.py), which returns the user_visits
rows the web app writes for every signed-in user: first and last request,
how many requests, searches, path queries and questions, and how many new
Cloudflare Access sign-ins.

  python logins_report.py              # print the last 24 hours
  python logins_report.py --hours 72
  python logins_report.py --telegram   # daily task: also send it (same bot and chat as pipeline_health_check.py)
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import requests

HERMES_SCRIPTS = r"C:\Users\johnk\AppData\Local\hermes\scripts"
sys.path.insert(0, HERMES_SCRIPTS)
import pg_secret  # noqa: E402

URL = os.environ.get("VISITS_URL", "https://sixdegrees.net/api/internal/user-links/visits")
COUNTS = ("searches", "paths", "questions", "sign_ins", "requests")


def fetch(hours):
    r = requests.get(URL, params={"hours": hours},
                     headers={"X-Sync-Token": pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"),
                              "User-Agent": "sixdegrees-logins-report"}, timeout=60)
    r.raise_for_status()
    return r.json()


def local(utc_text):
    """'2026-10-21 13:05:00' (UTC) -> this machine's local time, e.g. 'Wed 9:05 AM'."""
    t = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
    return t.strftime("%a %I:%M %p").replace(" 0", " ")


def people(rows):
    """One entry per user; a 24-hour window can cover two UTC days."""
    by = {}
    for r in rows:
        p = by.setdefault(r["email"], dict(r, **{k: 0 for k in COUNTS}))
        for k in COUNTS:
            p[k] += r[k] or 0
        p["first_at"], p["last_at"] = min(p["first_at"], r["first_at"]), max(p["last_at"], r["last_at"])
    return list(by.values())


def text(d):
    ps = people(d["rows"])
    hours = f"{d['hours']:g} h"
    if not ps:
        return f"sixdegrees.net: no one signed in during the last {hours}."
    others = [p for p in ps if not p["admin"]]
    lines = [f"sixdegrees.net, last {hours}: {len(others)} user(s)"
             + (f" plus {len(ps) - len(others)} admin account(s)" if len(ps) > len(others) else "")]
    for p in sorted(others, key=lambda p: p["last_at"], reverse=True) + [p for p in ps if p["admin"]]:
        who = f"{p['name']} <{p['email']}>" if p.get("name") else p["email"]
        did = [f"{p[k]} {label}" for k, label in (("searches", "searches"), ("paths", "path queries"),
                                                   ("questions", "questions")) if p[k]]
        lines.append(f"• {who}{' (admin)' if p['admin'] else ''}\n"
                     f"   {local(p['first_at'])} – {local(p['last_at'])}; "
                     f"{p['sign_ins']} sign-in(s), {p['requests']} requests"
                     + (f"; {', '.join(did)}" if did else ""))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--telegram", action="store_true", help="send the report to Telegram")
    args = ap.parse_args()
    if not args.telegram:
        print(text(fetch(args.hours)))
        return
    from pipeline_health_check import send_telegram
    try:
        msg = text(fetch(args.hours))
    except Exception as e:
        print(f"LOGINS_REPORT_FAILED: {e}")
        send_telegram(f"sixdegrees logins report failed: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
    print(msg)
    print("SENT" if send_telegram(msg) else "NOT_SENT")


if __name__ == "__main__":
    main()
