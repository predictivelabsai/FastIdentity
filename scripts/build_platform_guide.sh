#!/usr/bin/env bash
# Build docs/fastidentity_platform_guide.md -> PDF (A4 landscape, text left / screenshot right).
#
#   python scripts/capture_guide_screenshots.py   # refresh docs/img/*.png + walkthrough GIF
#   bash scripts/build_platform_guide.sh
#
# Requires: pandoc, weasyprint.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/docs"
SRC=fastidentity_platform_guide.md
BASE="${SRC%.md}"
GEN_DATE="$(date +%Y-%m-%d)"
sed -i -E "s|@bottom-right \{ content: \"[^\"]*\"|@bottom-right { content: \"${GEN_DATE} · identity.fastsme.com\"|" assets/guide.css
pandoc "$SRC" -s -o "$BASE.html" --from=markdown-implicit_figures --css assets/guide.css \
  --metadata pagetitle="FastIdentity — Platform Guide (${GEN_DATE})"
weasyprint "$BASE.html" "$BASE.pdf"
rm -f "$BASE.html"
echo "✓ docs/$BASE.pdf ($(du -h "$BASE.pdf" | cut -f1))"
