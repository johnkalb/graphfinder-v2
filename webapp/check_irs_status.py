#!/usr/bin/env python3
"""Check IRS bulk harvester progress and report status."""
import json, os, time
PROGRESS = os.path.join(os.path.dirname(__file__), "data", "irs_progress.json")

if not os.path.exists(PROGRESS):
    print("⏳ IRS harvester still initializing...")
else:
    with open(PROGRESS) as f:
        p = json.load(f)
    stage = p.get("stage", "?")
    pct = p.get("pct", 0)
    found = p.get("found", 0)
    parsed = p.get("parsed", 0)
    elapsed = p.get("elapsed", 0)
    
    if stage == "complete":
        print(f"✅ IRS Foundation Search COMPLETE")
        print(f"   {parsed} relationships added")
        print(f"   Time: {elapsed//60}m {elapsed%60}s")
    else:
        eta = ""
        if pct > 0 and elapsed > 0:
            remaining = (100 - pct) / pct * elapsed
            eta = f" | ETA: {remaining/60:.0f}m"
        print(f"🔄 IRS Foundation Search — {pct}% complete")
        print(f"   {parsed} relationships found so far")
        print(f"   Searching {found} organizations")
        print(f"   Elapsed: {elapsed//60}m{elapsed%60:02d}s{eta}")

# Also check if process is still running
import subprocess
if os.name == 'nt':
    result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq python.exe'], capture_output=True, text=True)
    running = 'harvest_irs_bulk' in result.stdout
else:
    result = subprocess.run(['pgrep', '-f', 'harvest_irs_bulk'], capture_output=True)
    running = result.returncode == 0

print(f"\n{'🟢 Running' if running else '🔴 Stopped'}")
 
