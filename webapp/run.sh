#!/bin/bash
# run.sh — Start the Network Pathfinder web app
cd "$(dirname "$0")"

echo "=== Network Pathfinder ==="
echo "Checking dependencies..."

# Install deps if needed
pip install -q fastapi uvicorn networkx 2>/dev/null

echo "Starting server at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

python -m uvicorn pathfinder:app --host 0.0.0.0 --port 8000 --reload --log-level info
 
