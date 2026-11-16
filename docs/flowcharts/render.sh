#!/usr/bin/env bash
# Regenerate every flowchart SVG from its Graphviz source.
# Requires Graphviz (`dot`):  apt-get install graphviz  |  brew install graphviz
set -euo pipefail
cd "$(dirname "$0")"
for f in src/*.dot; do
  out="$(basename "${f%.dot}").svg"
  dot -Tsvg "$f" -o "$out"
  echo "rendered $out"
done
