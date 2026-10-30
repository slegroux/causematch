#!/usr/bin/env bash
# Assembles the public website (GitHub Pages) into _site/ (or $1): site/ plus the logo, the
# hero image and the Nunito font, which stay where the app and the README use them.
# .github/workflows/pages.yml runs it and publishes the folder. To preview it locally:
# python3 -m http.server -d _site 8000 (opened as a file, the browser refuses the font).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-_site}"
rm -rf "$OUT"
mkdir -p "$OUT/assets/fonts"
cp -R site/. "$OUT/"
cp docs/assets/hero.png docs/assets/logo/*.svg "$OUT/assets/"
cp static/vendor/fonts/nunito-latin-wght-normal.woff2 static/vendor/fonts/OFL.txt "$OUT/assets/fonts/"
echo "Built $OUT"
