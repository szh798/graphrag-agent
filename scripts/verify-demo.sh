#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
BACKEND_DIR="$ROOT_DIR/backend"

BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

if [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  PYTHON_BIN="$BACKEND_DIR/.venv/bin/python"
elif [ -x "$BACKEND_DIR/.venv/Scripts/python.exe" ]; then
  PYTHON_BIN="$BACKEND_DIR/.venv/Scripts/python.exe"
else
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" - "$BACKEND_HOST" "$BACKEND_PORT" "$FRONTEND_HOST" "$FRONTEND_PORT" <<'PY'
import json
import sys
import urllib.request

backend_host, backend_port, frontend_host, frontend_port = sys.argv[1:5]
base = f"http://{backend_host}:{backend_port}/api/v1"
frontend = f"http://{frontend_host}:{frontend_port}/"
failures: list[str] = []


def fetch_json(path: str) -> dict:
    url = base + path
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def check(condition: bool, ok: str, fail: str) -> None:
    if condition:
        print(f"[PASS] {ok}")
    else:
        print(f"[FAIL] {fail}")
        failures.append(fail)


health = fetch_json("/health")
check(health.get("code") == 0, "health endpoint returned code=0", "health endpoint failed")
health_data = health.get("data") or {}
print(f"[INFO] health status: {health_data.get('status')}")
for name, component in sorted((health_data.get("components") or {}).items()):
    print(f"[INFO] component {name}: {component.get('status')}")

stats = fetch_json("/system/stats")
stats_data = stats.get("data") or {}
check(stats.get("code") == 0, "stats endpoint returned code=0", "stats endpoint failed")
check("total_documents" in stats_data, "stats include total_documents", "stats missing total_documents")
check("total_nodes" in stats_data, "stats include total_nodes", "stats missing total_nodes")
print(
    "[INFO] indexed docs={docs}, nodes={nodes}, edges={edges}".format(
        docs=stats_data.get("indexed_documents"),
        nodes=stats_data.get("total_nodes"),
        edges=stats_data.get("total_edges"),
    )
)

demo = fetch_json("/system/demo")
if demo.get("code") == 0:
    demo_data = demo.get("data") or {}
    check(len(demo_data.get("nodes", [])) > 0, "KG demo data is available", "KG demo data is empty")
    print(f"[INFO] demo nodes={len(demo_data.get('nodes', []))}, edges={len(demo_data.get('edges', []))}")
elif demo.get("code") == 3002:
    print("[WARN] KG demo data is not available. Index a document before showing the KG page.")
else:
    check(False, "KG demo endpoint is acceptable", f"KG demo endpoint returned code={demo.get('code')}")

with urllib.request.urlopen(frontend, timeout=10) as response:
    html = response.read().decode("utf-8", errors="replace")
check("<div id=\"root\"" in html, "frontend static page is reachable", "frontend page did not look like the Vite app")

if failures:
    print("\nGraphRAGAgent demo verification failed:")
    for failure in failures:
        print(f"  - {failure}")
    sys.exit(1)

print("\nGraphRAGAgent demo verification passed.")
PY
