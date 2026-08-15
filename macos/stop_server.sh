#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=../server_common.sh
source "$APP_ROOT/server_common.sh"

# Stop by PID file first, then sweep the port. The sweep matters because a
# reloader replaces its worker process, so the recorded PID is not always the
# one still holding the socket, and a leftover holder is what makes the next
# launch fail.
if [ -f "$PID_FILE" ]; then
  server_pid="$(tr -cd '0-9' < "$PID_FILE")"
  if [ -n "$server_pid" ] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    for _ in {1..20}; do
      kill -0 "$server_pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$PID_FILE"
fi

for pid in $(port_pids); do
  is_our_server "$pid" && kill "$pid" 2>/dev/null || true
done

exit 0
