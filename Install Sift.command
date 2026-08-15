#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"

pause_before_exit() {
  printf '\nPress any key to close this window.'
  read -r -n 1 _
}

on_error() {
  printf '\nSift setup could not finish. Review the message above, then run this installer again.\n'
  pause_before_exit
}
trap on_error ERR

printf '\nSift setup\n\n'

if ! command -v ollama >/dev/null 2>&1; then
  printf 'Ollama is required. Your browser will open the download page.\n'
  open "https://ollama.com/download"
  printf 'Install and open Ollama, then double-click Install Sift.command again.\n'
  exit 1
fi

# shellcheck source=server_common.sh
source "$APP_ROOT/server_common.sh"

ensure_ollama

if ! ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$MODEL"; then
  printf 'Downloading the default local AI model. This can take a few minutes.\n'
  ollama pull "$MODEL"
fi

EMBED_MODEL="${EMBED_MODEL:-qwen3-embedding:0.6b}"
if ! ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$EMBED_MODEL"; then
  printf 'Downloading the local search model.\n'
  ollama pull "$EMBED_MODEL"
fi

if ! ensure_venv; then
  open "https://www.python.org/downloads/macos/"
  printf 'Install Python 3.11 or newer, then run this installer again.\n'
  exit 1
fi
"$APP_ROOT/macos/build_app.sh"

printf '\nSift is ready. Finder will now reveal the app.\n'
open -R "$APP_ROOT/dist/Sift.app"
pause_before_exit
