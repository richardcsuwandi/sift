# Shared launch logic for ./run.sh (development) and macos/start_server.sh
# (the .app launcher). This file is sourced, never executed, and expects
# APP_ROOT to be set by the caller.
#
# The point of it is that both entry points bind the same port. Before this
# existed they each had their own idea of what to do when the port was taken,
# and the .app's answer was "assume it's fine", which is how a half-dead
# server got adopted and every request failed with "Failed to fetch".

if [ -z "${APP_ROOT:-}" ] || [ ! -d "$APP_ROOT/app" ]; then
  echo "server_common.sh: set APP_ROOT to the repository root before sourcing." >&2
  return 1 2>/dev/null || exit 1
fi

PORT="${SIFT_PORT:-8000}"
APP_URL="http://127.0.0.1:$PORT"
STATE_DIR="$APP_ROOT/.runtime"
PID_FILE="$STATE_DIR/server.pid"
LOG_FILE="$STATE_DIR/server.log"
MODEL="${OLLAMA_MODEL:-qwen3:4b}"

# Apps launched from Finder do not inherit the interactive shell's PATH.
# Include the standard Homebrew locations so Ollama, uv, and Python resolve
# exactly as they do from Terminal.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

mkdir -p "$STATE_DIR"

# Body of /api/health, or empty if nothing answered.
health_json() {
  /usr/bin/curl --silent --fail --max-time 1 "$APP_URL/api/health" 2>/dev/null
}

# PIDs listening on PORT, whoever they belong to.
port_pids() {
  lsof -tiTCP:"$PORT" -sTCP:LISTEN -n -P 2>/dev/null || true
}

# True if PID belongs to a server started from this checkout. Every kill in
# this file is guarded by it, so an unrelated program that happens to own the
# port is reported to the user instead of being killed out from under them.
#
# Three signals, because uvicorn's --reload worker is not recognisable by name:
# it re-execs as `python -c from multiprocessing.spawn import spawn_main ...`,
# and that worker is precisely the process that gets left holding the socket.
# Its interpreter path and working directory still point into this checkout.
is_our_server() {
  local pid="$1" cmd
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  [ -z "$cmd" ] && return 1
  case "$cmd" in
    *"$APP_ROOT"*) return 0 ;;
  esac
  printf '%s' "$cmd" | grep -q "uvicorn.*app\.main" && return 0
  lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | grep -qx "n$APP_ROOT"
}

# One of: ours | other-checkout | foreign | wedged | free
#
#   ours           a healthy Sift server from this directory, safe to reuse
#   other-checkout a healthy Sift server from a different copy of the repo
#   foreign        something else entirely is answering on this port
#   wedged         the port is held but /api/health does not answer
#   free           nothing is on the port
server_state() {
  local body
  body="$(health_json)"
  if [ -n "$body" ]; then
    case "$body" in
      *'"app":"sift"'*)
        case "$body" in
          *"\"root\":\"$APP_ROOT\""*) echo "ours" ;;
          *) echo "other-checkout" ;;
        esac
        ;;
      *) echo "foreign" ;;
    esac
    return
  fi
  if [ -n "$(port_pids)" ]; then echo "wedged"; else echo "free"; fi
}

# Free the port by stopping Sift servers holding it. Returns non-zero without
# killing anything if the holder is not ours.
reclaim_port() {
  local pid killed=0
  for pid in $(port_pids); do
    if is_our_server "$pid"; then
      kill "$pid" 2>/dev/null || true
      killed=1
    else
      echo "Port $PORT is held by PID $pid, which is not a Sift server:" >&2
      ps -o command= -p "$pid" >&2 || true
      echo "Stop it, or run with a different port: SIFT_PORT=8001" >&2
      return 1
    fi
  done
  [ "$killed" -eq 1 ] || return 0

  for _ in {1..40}; do
    [ -z "$(port_pids)" ] && { rm -f "$PID_FILE"; return 0; }
    sleep 0.1
  done

  # A uvicorn reloader whose worker has already died does not always exit on
  # SIGTERM, and that is exactly the state this function exists to clear.
  for pid in $(port_pids); do
    is_our_server "$pid" && kill -9 "$pid" 2>/dev/null || true
  done
  sleep 0.5
  rm -f "$PID_FILE"
  [ -z "$(port_pids)" ]
}

# Start uvicorn detached from this shell's session, logging to LOG_FILE.
#
# The .app runs its launcher through AppleScript's doShellScript, which kills
# the whole process group as soon as the script returns. `nohup` only blocks
# SIGHUP, so the server was being SIGTERMed a moment after it reported ready,
# and the app opened the browser onto a backend that had just died. Double
# forking into a new session puts the server out of that group's reach.
#
# The caller cannot use $! afterwards: the process it spawned exits straight
# away and the surviving server is a grandchild. Read the live PID out of
# /api/health once the server answers, which is more accurate anyway.
start_detached() {
  "$APP_ROOT/.venv/bin/python" - "$APP_ROOT" "$PORT" "$LOG_FILE" <<'PY'
import os
import sys

app_root, port, log_path = sys.argv[1], sys.argv[2], sys.argv[3]

if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)

os.chdir(app_root)
log = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(log, 1)
os.dup2(log, 2)
os.dup2(os.open(os.devnull, os.O_RDONLY), 0)

os.execv(
    sys.executable,
    [sys.executable, "-m", "uvicorn", "app.main:app",
     "--host", "127.0.0.1", "--port", port],
)
PY
}

# PID of the process actually answering, per its own health response.
serving_pid() {
  health_json | sed -n 's/.*"pid":\([0-9]*\).*/\1/p'
}

ensure_ollama() {
  if ! command -v ollama >/dev/null 2>&1; then
    echo "Ollama is not installed. Install it from https://ollama.com, then open Sift again." >&2
    return 1
  fi
  if ! pgrep -x ollama >/dev/null 2>&1; then
    nohup ollama serve >>"$STATE_DIR/ollama.log" 2>&1 &
    for _ in {1..30}; do
      ollama list >/dev/null 2>&1 && break
      sleep 0.5
    done
  fi
}

ensure_model() {
  if ! ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fxq "$MODEL"; then
    echo "The default model '$MODEL' is not installed. Run 'ollama pull $MODEL', then open Sift again." >&2
    echo "Any other Ollama model works too: OLLAMA_MODEL=<tag>" >&2
    return 1
  fi
}

ensure_venv() {
  [ -x "$APP_ROOT/.venv/bin/python" ] && return 0
  echo "Creating virtualenv..."
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.11 "$APP_ROOT/.venv"
    uv pip install --python "$APP_ROOT/.venv/bin/python" -r "$APP_ROOT/requirements.txt"
  elif command -v python3.11 >/dev/null 2>&1; then
    python3.11 -m venv "$APP_ROOT/.venv"
    "$APP_ROOT/.venv/bin/pip" install -q -r "$APP_ROOT/requirements.txt"
  else
    echo "Python 3.11 is required. Install Python or uv, then open Sift again." >&2
    return 1
  fi
}
