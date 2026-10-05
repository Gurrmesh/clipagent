#!/usr/bin/env bash
# One command to run ClipAgent Studio. Sets itself up the first time, then
# just starts. Safe to run again any time.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
say() { printf '\n  %s\n' "$*"; }

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  say "Python isn't installed."
  say "Mac:     brew install python@3.11"
  say "Windows: get it from python.org and tick 'Add python.exe to PATH'"
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  say "ffmpeg isn't installed — the renderer needs it."
  say "Mac:     brew install ffmpeg"
  say "Ubuntu:  sudo apt install ffmpeg"
  exit 1
fi

if [ ! -d .venv ]; then
  say "First run — setting up. This takes a minute."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Reinstall only when the requirements actually changed.
if [ ! -f .venv/.installed ] || [ requirements.txt -nt .venv/.installed ]; then
  say "Installing dependencies… (about 100 MB the first time)"
  pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  # A dropped download on a slow connection is the usual failure here, so the
  # message has to say what to do instead of dumping a traceback.
  if ! pip install --quiet --timeout 60 --retries 5 -r requirements.txt; then
    say "The install didn't finish — usually a dropped download."
    say "Run this script again; it picks up where it left off."
    exit 1
  fi
  touch .venv/.installed
fi

if [ ! -f .env ]; then
  cp .env.example .env
  say "Created .env for you. Open it, paste your two API keys, then run this again."
  exit 1
fi

if ! grep -qE '^(ANTHROPIC_API_KEY=sk-|OPENAI_API_KEY=sk-|WHISPER_API_KEY=)' .env; then
  say "No keys found in .env yet — open it and paste them in."
  say "The app will still start, but it can't run a job without them."
fi

say "ClipAgent Studio is starting on http://localhost:$PORT  (Ctrl+C to stop)"
( sleep 2; (open "http://localhost:$PORT" || xdg-open "http://localhost:$PORT") >/dev/null 2>&1 ) &
exec python -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
