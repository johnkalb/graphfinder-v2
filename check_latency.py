"""Print the live site's response times per route (2026-10-18).

Reads /api/internal/user-links/latency (USER_LINKS_SYNC_TOKEN header, a
Cloudflare Bypass path like sync_user_links.py's), which summarises the
request_logs table the web app writes for every request.

  python check_latency.py                  # last 24 hours
  python check_latency.py --hours 2        # just the last two hours
  python check_latency.py --alert-ms 3000  # daily task: Telegram if any request took > 3 s

--alert-ms sends a Telegram message (same bot and chat as
pipeline_health_check.py) only when a visitor-facing route had a request over
the threshold, the app uses over 85% of the container's memory, or the check
itself fails; otherwise it just prints.
"""
import argparse
import os
import sys

import requests

HERMES_SCRIPTS = r"C:\Users\johnk\AppData\Local\hermes\scripts"
sys.path.insert(0, HERMES_SCRIPTS)
import pg_secret  # noqa: E402

URL = os.environ.get("LATENCY_URL", "https://sixdegrees.net/api/internal/user-links/latency")
WATCH = ("/api/search", "/api/path", "/api/entity", "/")
NOT_VISITOR = ("/api/internal", "/admin", "/api/admin", "/debug")   # our own tooling
MEM_ALERT = 0.85   # alert when the app's processes use more than this share of the container's memory


def fetch(hours, slow_ms):
    r = requests.get(URL, params={"hours": hours, "slow_ms": slow_ms},
                     headers={"X-Sync-Token": pg_secret.load_secret("USER_LINKS_SYNC_TOKEN"),
                              "User-Agent": "sixdegrees-latency-check"}, timeout=60)
    r.raise_for_status()
    return r.json()


def report(d):
    print(f"Last {d['hours']:g} h: {d['rows']:,} requests\n")
    print(f"{'route':42} {'n':>6} {'p50':>8} {'p90':>8} {'p99':>8} {'max':>8} {'slow':>5}   (ms; slow = over {d['slow_ms']:g})")
    for x in d["routes"]:
        mark = " <" if x["route"] in WATCH else ""
        print(f"{x['route'][:42]:42} {x['n']:>6} {x['p50']:>8.0f} {x['p90']:>8.0f} {x['p99']:>8.0f} "
              f"{x['max']:>8.0f} {x['slow']:>5}{mark}")
    print("\nSlowest requests:")
    for x in d["slowest"][:15]:
        print(f"  {x['ms']:>7} ms  {x['status']}  {x['at']}  {x['route']}")
    m = d.get("memory") or {}
    if m:
        print(f"\nMemory: app processes {memory_use(m)[0]:,} MB of {memory_use(m)[1] or '?'} MB; "
              f"container {m.get('cgroup_used_mb')} / {m.get('cgroup_limit_mb')} MB (includes file cache); "
              f"MemAvailable {m.get('MemAvailable_mb')} of {m.get('MemTotal_mb')} MB")
        for p in m.get("processes", []):
            print(f"  {p['role']:5} pid {p['pid']:>6}  pss {p['pss_mb']} MB  rss {p['rss_mb']} MB")


def memory_use(m):
    """(MB used by the app and its path workers, MB limit). Pss, not the
    cgroup's count, which includes reclaimable cache of the graph files."""
    used = sum(p["pss_mb"] or p["rss_mb"] or 0 for p in m.get("processes", []))
    limit, total = m.get("cgroup_limit_mb"), m.get("MemTotal_mb")
    if not limit or (total and limit > total):   # cgroup v1 reports "no limit" as ~8e12 MB
        limit = total
    return used, limit


def alert_text(d):
    """The Telegram message, or None when nothing visitor-facing was slow."""
    slow = [x for x in d["routes"] if x["slow"] and not x["route"].startswith(NOT_VISITOR)]
    used, limit = memory_use(d.get("memory") or {})
    mem_line = (f"sixdegrees memory: app uses {used:,} MB of {limit:,} MB ({used / limit:.0%})"
                if limit and used > MEM_ALERT * limit else None)
    if not slow:
        return mem_line
    secs = d["slow_ms"] / 1000
    lines = ([mem_line] if mem_line else []) + [
        f"sixdegrees latency: {sum(x['slow'] for x in slow)} request(s) over {secs:g} s "
        f"in the last {d['hours']:g} h"]
    for x in sorted(slow, key=lambda x: -x["slow"]):
        lines.append(f"  {x['route']}: {x['slow']} of {x['n']} slow, p90 {x['p90'] / 1000:.1f} s, "
                     f"max {x['max'] / 1000:.1f} s")
    worst = [x for x in d["slowest"] if not x["route"].startswith(NOT_VISITOR)][:5]
    if worst:
        lines.append("Slowest:")
        lines += [f"  {x['ms'] / 1000:.1f} s  {x['route']}  {x['at']}" for x in worst]
    lines.append("Details: python check_latency.py")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--alert-ms", type=float, help="send a Telegram alert if any request took longer")
    args = ap.parse_args()
    slow_ms = args.alert_ms or 3000
    if not args.alert_ms:
        report(fetch(args.hours, slow_ms))
        return
    from pipeline_health_check import send_telegram
    try:
        d = fetch(args.hours, slow_ms)
    except Exception as e:
        print(f"LATENCY_CHECK_FAILED: {e}")
        send_telegram(f"sixdegrees latency check failed: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)
    report(d)
    text = alert_text(d)
    if text is None:
        print(f"\nOK: no visitor request over {slow_ms / 1000:g} s")
        return
    print("\n" + text)
    print("ALERT_SENT" if send_telegram(text) else "ALERT_NOT_SENT")


if __name__ == "__main__":
    main()
