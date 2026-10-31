#!/usr/bin/env bash
# Make Causematch's narrated demo video and install it on the website.
#
#   scripts/video/make_demo_video.sh [STEP ...]     (no step: all of them, in order)
#
# Steps: setup (tools, the TTS virtualenv and models), voice (narration lines), record
# (the app, live, on a new throwaway database), compose (1080p video, loudness), install
# (site/assets/demo.mp4, demo-poster.jpg and the captions demo.vtt). See scripts/video/README.md.
#
# Outside the repo: CAUSEMATCH_VIDEO_CACHE (default ~/.cache/causematch: the TTS virtualenv
# and the espeak-ng data) and CAUSEMATCH_VIDEO_WORK (default $CACHE/video-work: voice,
# recordings, the finished video; never inside the repo). CAUSEMATCH_VIDEO_VOICE (default
# af_heart) is the Kokoro voice. CAUSEMATCH_VIDEO_PORT (default 5190) is the demo
# server's port. Nothing here touches a real database: record uses scripts/demo.py's
# throwaway one, with invented data only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/scripts/video"
CACHE="${CAUSEMATCH_VIDEO_CACHE:-$HOME/.cache/causematch}"
WORK="${CAUSEMATCH_VIDEO_WORK:-$CACHE/video-work}"
PORT="${CAUSEMATCH_VIDEO_PORT:-5190}"
VENV="$CACHE/tts-venv"
TTS_PY="$VENV/bin/python"
VOICE="${CAUSEMATCH_VIDEO_VOICE:-af_heart}"
MARK=".causematch-video-work"
cd "$ROOT"

case "$(cd "$WORK" 2>/dev/null && pwd || echo "$WORK")/" in
  "$ROOT"/*) echo "CAUSEMATCH_VIDEO_WORK must be outside the repo: $WORK" >&2; exit 1 ;;
esac

say() { printf '\n== %s\n' "$*"; }

need() {
  for tool in ffmpeg ffprobe uv curl lsof; do
    command -v "$tool" >/dev/null || { echo "$tool is missing (ffmpeg: brew install ffmpeg; uv: https://docs.astral.sh/uv/)" >&2; exit 1; }
  done
}

setup() {
  say "setup"
  need
  uv sync --quiet
  uv run playwright install chromium
  mkdir -p "$CACHE" "$WORK"
  touch "$WORK/$MARK"  # record deletes $WORK/take and rec.db: only a folder made here
  if [ ! -x "$TTS_PY" ]; then
    uv venv --quiet -p 3.12 "$VENV"
  fi
  if ! "$TTS_PY" -c "import kokoro, soundfile" 2>/dev/null; then
    VIRTUAL_ENV="$VENV" uv pip install --quiet kokoro==0.9.4 soundfile==0.14.0
  fi
  # misaki (Kokoro's text front end) wants a spaCy model and cannot pip-install it into a
  # uv virtualenv, so install the one matching the installed spaCy from its GitHub release
  if ! "$TTS_PY" -c "import en_core_web_sm" 2>/dev/null; then
    minor="$("$TTS_PY" -c 'import spacy; print(".".join(spacy.__version__.split(".")[:2]))')"
    VIRTUAL_ENV="$VENV" uv pip install --quiet \
      "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-$minor.0/en_core_web_sm-$minor.0-py3-none-any.whl"
  fi
}

voice() {
  say "voice ($VOICE): the first run downloads Kokoro's model (about 330 MB) from Hugging Face"
  rm -f "$WORK/voice/durations.json"  # a failed run must leave no stale list behind
  "$TTS_PY" "$HERE/tts.py" "$HERE/beats.json" "$WORK/voice" "$CACHE" "$VOICE" \
    >"$WORK/voice.log" 2>&1 || { cat "$WORK/voice.log" >&2; echo "tts.py failed" >&2; exit 1; }
  grep -v -i -E "warn|torch\.|deprecat|^\s*$" "$WORK/voice.log" || true
  test -s "$WORK/voice/durations.json"
}

record() {
  say "record (about 3 minutes)"
  if lsof -ti "tcp:$PORT" >/dev/null 2>&1; then
    echo "port $PORT is in use: set CAUSEMATCH_VIDEO_PORT" >&2
    exit 1
  fi
  need
  [ -f "$WORK/$MARK" ] || { echo "$WORK wasn't made by this script: run setup first" >&2; exit 1; }
  rm -rf "$WORK/take" "$WORK/rec.db"*
  mkdir -p "$WORK/take"
  uv run python "$HERE/serve.py" "$PORT" "$WORK/rec.db" >"$WORK/server.log" 2>&1 &
  server=$!
  trap 'kill $server 2>/dev/null || true' EXIT
  up=""
  for _ in $(seq 1 120); do
    curl -s -o /dev/null "http://127.0.0.1:$PORT/login" && { up=1; break; }
    kill -0 $server 2>/dev/null || { cat "$WORK/server.log" >&2; echo "the server stopped" >&2; exit 1; }
    sleep 0.5
  done
  [ -n "$up" ] || { cat "$WORK/server.log" >&2; echo "the server never answered on port $PORT" >&2; exit 1; }
  uv run python "$HERE/record.py" "http://127.0.0.1:$PORT" "$WORK/server.log" \
    "$HERE/beats.json" "$WORK/voice/durations.json" "$WORK/rec.db" "$WORK/take"
  kill $server 2>/dev/null || true
  wait $server 2>/dev/null || true
  trap - EXIT
}

compose() {
  say "compose"
  need
  uv run python "$HERE/compose.py" "$HERE/beats.json" "$WORK/voice" "$WORK/take" "$WORK/demo.mp4"
}

install() {
  say "install"
  cp "$WORK/demo.mp4" "$ROOT/site/assets/demo.mp4"
  cp "$WORK/demo-poster.jpg" "$ROOT/site/assets/demo-poster.jpg"
  cp "$WORK/demo.vtt" "$ROOT/site/assets/demo.vtt"
  ls -l "$ROOT/site/assets/demo.mp4" "$ROOT/site/assets/demo-poster.jpg" "$ROOT/site/assets/demo.vtt"
}

steps=("$@")
[ ${#steps[@]} -gt 0 ] || steps=(setup voice record compose install)
for s in "${steps[@]}"; do
  case "$s" in
    setup | voice | record | compose | install) "$s" ;;
    *) echo "unknown step: $s (setup voice record compose install)" >&2; exit 2 ;;
  esac
done
