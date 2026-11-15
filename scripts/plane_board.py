#!/usr/bin/env python3
"""Plane board helper — stdlib only.

Reads config from scripts/plane.env (same directory; gitignored — never commit it).

Usage:
  python3 scripts/plane_board.py list
  python3 scripts/plane_board.py read <substring-of-issue-name>
  python3 scripts/plane_board.py create --name "..." [--description "..."] [--priority high|medium|low]
  python3 scripts/plane_board.py comment <substring> --body "..."
  python3 scripts/plane_board.py update <substring> --state <unstarted|started|completed>
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

ENV = {}
with open(os.path.join(HERE, "plane.env")) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip()

BASE = ENV.get("PLANE_BASE_URL", "http://localhost:8080").rstrip("/")
WS = ENV.get("PLANE_WORKSPACE", "my-projects")
TOKEN = ENV.get("PLANE_TOKEN", "")
PROJECT = ENV.get("PLANE_PROJECT", "HOO")


def api(method, path, body=None):
    url = f"{BASE}/api/v1/workspaces/{WS}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", TOKEN)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode() or "{}")


def _res(d):
    return d.get("results", []) if isinstance(d, dict) else (d or [])


def project_id():
    d = api("GET", "/projects/")
    for p in _res(d):
        if p.get("identifier") == PROJECT:
            return p["id"]
    raise SystemExit(f"project {PROJECT!r} not found on board")


def states(pid):
    return _res(api("GET", f"/projects/{pid}/states/"))


def resolve_state(pid, s):
    for st in states(pid):
        if s in (st.get("group"), st.get("name"), st.get("id")):
            return st["id"]
    raise SystemExit(f"unknown state {s!r}")


def find(pid, substr):
    d = api("GET", f"/projects/{pid}/issues/")
    for i in _res(d):
        if substr.lower() in (i.get("name") or "").lower():
            return i
    return None


def strip_html(s):
    return re.sub(r"<[^>]+>", "", s or "")


def main(argv):
    if not argv or argv[0] in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]

    def get(*names):
        for n in names:
            if n in argv:
                i = argv.index(n)
                if i + 1 < len(argv):
                    return argv[i + 1]
        return None

    pid = project_id()
    state_map = {st.get("id"): (st.get("group") or st.get("name")) for st in states(pid)}

    if cmd == "list":
        d = api("GET", f"/projects/{pid}/issues/")
        for i in _res(d):
            print(f"- {i.get('name')}  [{state_map.get(i.get('state'), i.get('state'))}]")
        return 0

    if cmd == "create":
        name = get("--name")
        if not name:
            print("--name required")
            return 1
        desc = get("--description") or ""
        prio = get("--priority")
        body = {"name": name, "state": resolve_state(pid, "unstarted")}
        if desc:
            body["description_html"] = "".join(f"<p>{ln}</p>" for ln in desc.splitlines())
        if prio:
            body["priority"] = prio
        r = api("POST", f"/projects/{pid}/issues/", body)
        if r.get("id"):
            print("created:", r["name"], "->", r["id"])
            return 0
        print("create failed:", json.dumps(r)[:300])
        return 1

    sub = argv[1] if len(argv) > 1 else None
    issue = find(pid, sub) if sub else None
    if issue is None:
        print(f"no issue matching {sub!r}")
        return 1
    iid = issue["id"]

    if cmd == "read":
        print("NAME:", issue.get("name"))
        print("STATE:", state_map.get(issue.get("state"), issue.get("state")))
        print("DESCRIPTION:", strip_html(issue.get("description_html")))
        print("--- comments ---")
        for c in _res(api("GET", f"/projects/{pid}/issues/{iid}/comments/")):
            print("•", strip_html(c.get("comment_html")))
        return 0

    if cmd == "comment":
        body = get("--body")
        if not body:
            print("--body required")
            return 1
        api("POST", f"/projects/{pid}/issues/{iid}/comments/",
            {"comment_html": "".join(f"<p>{ln}</p>" for ln in body.splitlines())})
        print("comment posted to:", issue.get("name"))
        return 0

    if cmd == "update":
        s = get("--state")
        if not s:
            print("--state required")
            return 1
        api("PATCH", f"/projects/{pid}/issues/{iid}/", {"state": resolve_state(pid, s)})
        print(f"updated {issue.get('name')} -> {s}")
        return 0

    print(f"unknown command {cmd!r}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
