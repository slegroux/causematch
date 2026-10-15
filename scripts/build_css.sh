#!/usr/bin/env bash
# Builds static/vendor/app.css: the Tailwind classes used in main.py, app/, static/*.js and
# MonsterUI's components, compiled ahead of time (the Play CDN compiled them in the browser,
# which a CSP without 'unsafe-inline' forbids). The output is committed, so running the app
# needs no build; run this again after adding Tailwind classes, and commit the result.
#
# Downloads the pinned Tailwind standalone CLI once (no Node) and checks its SHA-256 against
# the release's sha256sums.txt, copied below.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=3.4.17
case "$(uname -s)-$(uname -m)" in
Darwin-arm64) ASSET=tailwindcss-macos-arm64 SHA=a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3 ;;
Darwin-x86_64) ASSET=tailwindcss-macos-x64 SHA=6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3 ;;
Linux-x86_64) ASSET=tailwindcss-linux-x64 SHA=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;;
Linux-aarch64) ASSET=tailwindcss-linux-arm64 SHA=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;;
*) echo "no pinned Tailwind CLI for $(uname -s)-$(uname -m)" >&2 && exit 1 ;;
esac

sha256() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }

BIN="${XDG_CACHE_HOME:-$HOME/.cache}/causematch/$ASSET-$VERSION"
if [ ! -x "$BIN" ] || [ "$(sha256 "$BIN")" != "$SHA" ]; then
	mkdir -p "$(dirname "$BIN")"
	TMP="$(mktemp)"
	curl -sSfL -o "$TMP" "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$ASSET"
	if [ "$(sha256 "$TMP")" != "$SHA" ]; then
		echo "checksum mismatch for $ASSET $VERSION: refusing to run it" >&2
		rm -f "$TMP"
		exit 1
	fi
	chmod +x "$TMP"
	mv "$TMP" "$BIN"
fi

MONSTERUI_DIR="$(uv run python -c 'import monsterui, os; print(os.path.dirname(monsterui.__file__))')" \
	"$BIN" --config tailwind.config.js --output static/vendor/app.css --minify
