#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_ROOT"
# shellcheck source=server_common.sh
source "$APP_ROOT/server_common.sh"

ensure_ollama
ensure_model
ensure_venv

# Take the port back rather than failing to bind. Double-clicking Sift.app and
# then running this script is the normal way to end up with two servers, and
# the one you started last is the one you meant.
case "$(server_state)" in
  free) ;;
  ours|other-checkout|wedged)
    echo "Stopping the server already on port $PORT..."
    reclaim_port || exit 1
    ;;
  foreign)
    echo "Port $PORT is answering, but not as Sift. Stop it, or set SIFT_PORT=8001." >&2
    exit 1
    ;;
esac

unload_all_models() {
  ollama ps 2>/dev/null | awk 'NR>1 {print $1}' | while read -r m; do
    [ -n "$m" ] && ollama stop "$m" >/dev/null 2>&1
  done
}

on_int() {
  trap '' INT TERM
  echo "Ctrl+C received, unloading Ollama models in the background..."
  unload_all_models &
}
trap on_int INT TERM

cleanup() {
  trap '' INT TERM
  rm -f "$PID_FILE"
  unload_all_models
}
trap cleanup EXIT

printf '%s\n' "$$" > "$PID_FILE"

echo "Starting Sift at $APP_URL"
echo "Press Ctrl+C to stop."

exec ./.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port "$PORT"
