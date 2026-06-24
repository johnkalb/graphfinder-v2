#!/usr/bin/env python3
"""Daily deceased-status updater for sixdegrees.net.

Re-queries Wikidata P570 (date of death) for our verified-QID people
(data/qid_map.jsonl), compares against the deployed data/deceased.json, and if
anything changed, updates BOTH copies (webapp/data + data/), commits, and pushes
to the deploy branch so DigitalOcean serves the new living/deceased status.

deceased.json is loaded independently at runtime (no graph rebuild needed), so
this is a light update. Pre-1900 "deaths" are treated as bad-namesake QID matches
and skipped (same guard used when the map was built).

Designed for an unattended daily cron:
  - SILENT (no output) when nothing changed -> watchdog pattern, no spurious pings.
  - Prints a short report ONLY when new deaths are detected (which gets delivered).
  - Self-contained: resolves all paths from $HOME, sets PYTHONPATH, uses project venv.

Run:  cd ~ && PYTHONPATH="$HOME" ./venv/Scripts/python update_deceased_daily.py
"""
import os, sys, json, time, subprocess
import requests

HOME = os.path.expanduser("~")
REPO = os.path.join(HOME, "graphfinder-clean")
QID_MAP = os.path.join(HOME, "qid_map.jsonl")
DECEASED_LOCAL = os.path.join(HOME, "webapp", "data", "deceased.json")
DECEASED_REPO_WEBAPP = os.path.join(REPO, "webapp", "data", "deceased.json")
DECEASED_REPO_DATA = os.path.join(REPO, "data", "deceased.json")
BRANCH = "scoring-model"

ENDPOINT = "https://query.wikidata.org/sparql"
HEADERS = {"User-Agent": "SixDegreesGraph/1.0 (research; sixdegrees.net)",
           "Accept": "application/sparql-results+json"}
BATCH = 60


def fetch_deceased():
    qid_to_name = {}
    with open(QID_MAP, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            qid_to_name[r["qid"]] = r["name"]
    qids = list(qid_to_name.keys())
    deceased = {}
    for i in range(0, len(qids), BATCH):
        batch = qids[i:i + BATCH]
        values = " ".join(f"wd:{q}" for q in batch)
        query = f"SELECT ?person ?death WHERE {{ VALUES ?person {{ {values} }} ?person wdt:P570 ?death . }}"
        rows = []
        for attempt in range(4):
            try:
                resp = requests.get(ENDPOINT, params={"query": query, "format": "json"},
                                    headers=HEADERS, timeout=90)
                if resp.status_code == 429:
                    time.sleep(4 * (attempt + 1)); continue
                rows = resp.json()["results"]["bindings"]
                break
            except Exception:
                time.sleep(3 * (attempt + 1))
        for row in rows:
            qid = row.get("person", {}).get("value", "").rsplit("/", 1)[-1]
            death = row.get("death", {}).get("value", "")[:10]
            name = qid_to_name.get(qid)
            # skip pre-1900 deaths = bad namesake QID matches
            if name and death and death[:4].isdigit() and int(death[:4]) >= 1900:
                deceased[name.lower()] = death
        time.sleep(1.0)
    return deceased


def load_existing():
    try:
        with open(DECEASED_LOCAL, encoding="utf-8") as f:
            return {k.lower(): v for k, v in json.load(f).items()}
    except Exception:
        return {}


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)


def main():
    if not os.path.exists(QID_MAP):
        print(f"ERROR: {QID_MAP} not found", file=sys.stderr)
        sys.exit(1)

    new = fetch_deceased()
    old = load_existing()

    # Guard against a failed/empty query nuking the list
    if not new:
        print("ERROR: death query returned nothing — refusing to overwrite existing list.", file=sys.stderr)
        sys.exit(1)

    newly_dead = {k: v for k, v in new.items() if k not in old}
    # We only ever ADD deaths; never resurrect (a removed death = data glitch, ignore).
    merged = dict(old)
    merged.update(new)

    if not newly_dead:
        # No change -> SILENT (watchdog pattern). Cron delivers nothing.
        return

    # Write updated list to all three locations
    payload = json.dumps(merged, indent=0, sort_keys=True)
    for path in (DECEASED_LOCAL, DECEASED_REPO_WEBAPP, DECEASED_REPO_DATA):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)

    # Commit + push only the deceased.json files on the deploy branch
    git("checkout", BRANCH)
    git("add", "webapp/data/deceased.json", "data/deceased.json")
    names = ", ".join(sorted(newly_dead))
    c = git("commit", "-m", f"Daily deceased update: +{len(newly_dead)} ({names[:120]})")
    if "nothing to commit" in (c.stdout + c.stderr):
        return
    push = git("push", "fresh", f"{BRANCH}:{BRANCH}")
    ok = push.returncode == 0

    # Report (this is the ONLY non-silent path -> gets delivered)
    print(f"Deceased status updated: {len(newly_dead)} newly recorded as deceased.")
    for k, v in sorted(newly_dead.items()):
        print(f"  - {k.title()} (d. {v})")
    print(f"\nDeploy push to {BRANCH}: {'OK — DigitalOcean will redeploy' if ok else 'FAILED: ' + push.stderr[-300:]}")
    print("Living-only pathfinding will now exclude these as intermediaries.")


if __name__ == "__main__":
    main()
