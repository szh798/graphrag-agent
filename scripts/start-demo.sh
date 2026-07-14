#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
RUNTIME_DIR="$ROOT_DIR/.demo-runtime"
LOG_DIR="$RUNTIME_DIR/logs"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

mkdir -p "$LOG_DIR"

log() {
  printf '[GraphRAG demo] %s\n' "$1"
}

fail() {
  printf '[GraphRAG demo] ERROR: %s\n' "$1" >&2
  exit 1
}

pick_python() {
  if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$BACKEND_DIR/.venv/bin/python"
    return
  fi

  if [ -x "$BACKEND_DIR/.venv/Scripts/python.exe" ]; then
    printf '%s\n' "$BACKEND_DIR/.venv/Scripts/python.exe"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$(command -v python3)"
    return
  fi

  fail "No Python runtime found. Bring backend/.venv or install Python 3.12+."
}

wait_for_url() {
  local url="$1"
  local label="$2"
  "$PYTHON_BIN" - "$url" "$label" <<'PY'
import sys
import time
import urllib.request

url = sys.argv[1]
label = sys.argv[2]
last_error = ""
for _ in range(40):
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status < 500:
                print(f"{label} is ready: {url}")
                sys.exit(0)
    except Exception as exc:
        last_error = str(exc)
    time.sleep(0.5)

print(f"{label} did not become ready: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

start_detached() {
  local pid_file="$1"
  local log_file="$2"
  shift 2

  "$PYTHON_BIN" - "$pid_file" "$log_file" "$@" <<'PY'
import os
import subprocess
import sys
from pathlib import Path

pid_file = Path(sys.argv[1])
log_file = Path(sys.argv[2])
cmd = sys.argv[3:]

log_file.parent.mkdir(parents=True, exist_ok=True)
with open(log_file, "ab", buffering=0) as log:
    process = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
        cwd=os.getcwd(),
    )

pid_file.write_text(str(process.pid), encoding="utf-8")
PY
}

ensure_backend_env() {
  if [ -f "$BACKEND_DIR/.env" ]; then
    return
  fi

  if [ -f "$BACKEND_DIR/.env.offline.example" ]; then
    cp "$BACKEND_DIR/.env.offline.example" "$BACKEND_DIR/.env"
    log "Created backend/.env from .env.offline.example."
    return
  fi

  fail "backend/.env is missing and no offline template was found."
}

ensure_backend_dependencies() {
  if "$PYTHON_BIN" -c "import fastapi, uvicorn, langextract" >/dev/null 2>&1; then
    return
  fi

  if [ -d "$ROOT_DIR/offline/wheels" ]; then
    log "Installing backend dependencies from offline/wheels."
    "$PYTHON_BIN" -m pip install --no-index --find-links "$ROOT_DIR/offline/wheels" -r "$BACKEND_DIR/requirements.txt"
    return
  fi

  if command -v uv >/dev/null 2>&1; then
    log "Installing backend dependencies with uv."
    (
      cd "$BACKEND_DIR"
      uv pip install -r requirements.txt
    )
    return
  fi

  fail "Backend dependencies are missing. Package backend/.venv or offline/wheels before the interview."
}

ensure_frontend_dist() {
  if [ -f "$FRONTEND_DIR/dist/index.html" ]; then
    return
  fi

  if [ -d "$FRONTEND_DIR/node_modules" ] && command -v npm >/dev/null 2>&1; then
    log "Building frontend/dist."
    (
      cd "$FRONTEND_DIR"
      npm run build
    )
    return
  fi

  fail "frontend/dist is missing. Build it before packaging the offline demo."
}

start_backend() {
  local pid_file="$RUNTIME_DIR/backend.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" >/dev/null 2>&1; then
    log "Backend already running with PID $(cat "$pid_file")."
    return
  fi

  log "Starting backend on http://$BACKEND_HOST:$BACKEND_PORT."
  (
    cd "$BACKEND_DIR"
    start_detached "$pid_file" "$LOG_DIR/backend.log" "$PYTHON_BIN" -m uvicorn main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT"
  )
}

start_frontend() {
  local pid_file="$RUNTIME_DIR/frontend.pid"
  if [ -f "$pid_file" ] && kill -0 "$(cat "$pid_file")" >/dev/null 2>&1; then
    log "Frontend already running with PID $(cat "$pid_file")."
    return
  fi

  log "Serving frontend on http://$FRONTEND_HOST:$FRONTEND_PORT."
  (
    cd "$FRONTEND_DIR/dist"
    start_detached "$pid_file" "$LOG_DIR/frontend.log" "$PYTHON_BIN" -m http.server "$FRONTEND_PORT" --bind "$FRONTEND_HOST"
  )
}

PYTHON_BIN="$(pick_python)"
log "Using Python: $PYTHON_BIN"

ensure_backend_env
ensure_backend_dependencies
ensure_frontend_dist
start_backend
start_frontend

wait_for_url "http://$BACKEND_HOST:$BACKEND_PORT/api/v1/health" "Backend"
wait_for_url "http://$FRONTEND_HOST:$FRONTEND_PORT/" "Frontend"

cat <<EOF

GraphRAGAgent offline demo is running.

Frontend: http://$FRONTEND_HOST:$FRONTEND_PORT
Backend:  http://$BACKEND_HOST:$BACKEND_PORT/api/v1
Health:   http://$BACKEND_HOST:$BACKEND_PORT/api/v1/health

Logs:
  $LOG_DIR/backend.log
  $LOG_DIR/frontend.log

Next:
  ./scripts/verify-demo.sh
  ./scripts/stop-demo.sh
EOF
