#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
RUNTIME_DIR="$ROOT_DIR/.demo-runtime"

stop_pid_file() {
  local label="$1"
  local pid_file="$2"

  if [ ! -f "$pid_file" ]; then
    printf '[GraphRAG demo] %s is not recorded as running.\n' "$label"
    return
  fi

  local pid
  pid="$(cat "$pid_file")"
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill "$pid"
    printf '[GraphRAG demo] Stopped %s PID %s.\n' "$label" "$pid"
  else
    printf '[GraphRAG demo] %s PID %s was already stopped.\n' "$label" "$pid"
  fi
  rm -f "$pid_file"
}

stop_pid_file "backend" "$RUNTIME_DIR/backend.pid"
stop_pid_file "frontend" "$RUNTIME_DIR/frontend.pid"
