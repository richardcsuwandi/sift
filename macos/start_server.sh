#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../server_common.sh
source "$APP_ROOT/server_common.sh"

server_is_ready() {
  [ "$(server_state)" = "ours" ]
}

# Reuse a healthy server from this checkout; clear anything else off the port.
# Reusing whatever answered the port was the old behaviour, and it meant a
# reloader that had lost its worker got adopted and served nothing.
case "$(server_state)" in
  ours)
    exit 0
    ;;
  wedged|other-checkout)
    reclaim_port || exit 1
    ;;
  foreign)
    echo "Port $PORT is in use by another program. Quit it, then open Sift again." >&2
    exit 1
    ;;
esac

ensure_ollama
ensure_model
ensure_venv

cd "$APP_ROOT"
start_detached

for _ in {1..60}; do
  if server_is_ready; then
    printf '%s\n' "$(serving_pid)" > "$PID_FILE"
    exit 0
  fi
  sleep 0.25
done

reclaim_port || true
rm -f "$PID_FILE"
echo "Sift did not become ready in time. See $LOG_FILE for details." >&2
exit 1
