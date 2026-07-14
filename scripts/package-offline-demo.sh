#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/offline-packages}"
PACKAGE_NAME="${PACKAGE_NAME:-GraphRAGAgent-offline-demo-$STAMP}"
PACKAGE_DIR="$OUTPUT_DIR/$PACKAGE_NAME"
ARCHIVE_PATH="$OUTPUT_DIR/$PACKAGE_NAME.tar.gz"

mkdir -p "$OUTPUT_DIR"
rm -rf "$PACKAGE_DIR"
mkdir -p "$PACKAGE_DIR"

EXCLUDES=(
  --exclude ".DS_Store"
  --exclude "__pycache__/"
  --exclude "*.pyc"
  --exclude "*.pyo"
  --exclude ".demo-runtime/"
  --exclude "offline-packages/"
  --exclude "backend/.env"
  --exclude "frontend/node_modules/"
)

if [ "${INCLUDE_NODE_MODULES:-0}" = "1" ]; then
  EXCLUDES=(
    --exclude ".DS_Store"
    --exclude "__pycache__/"
    --exclude "*.pyc"
    --exclude "*.pyo"
    --exclude ".demo-runtime/"
    --exclude "offline-packages/"
    --exclude "backend/.env"
  )
fi

if [ ! -f "$ROOT_DIR/frontend/dist/index.html" ]; then
  printf '[GraphRAG package] frontend/dist is missing. Build the frontend before packaging.\n' >&2
  exit 1
fi

if [ ! -d "$ROOT_DIR/backend/.venv" ]; then
  printf '[GraphRAG package] backend/.venv is missing. The package can still be built, but the target machine must install backend dependencies offline.\n'
fi

rsync -a "${EXCLUDES[@]}" "$ROOT_DIR/" "$PACKAGE_DIR/"

cat > "$PACKAGE_DIR/RUN-DEMO.txt" <<'TXT'
GraphRAGAgent Offline Demo

1. Open a terminal in this folder.
2. Run: ./scripts/start-demo.sh
3. Open the frontend URL printed by the script.
4. Run: ./scripts/verify-demo.sh
5. Stop it with: ./scripts/stop-demo.sh

The package intentionally excludes backend/.env. On first start, the script
creates backend/.env from backend/.env.offline.example. Add real cloud or local
model credentials only on the target machine when you need QA or new indexing.
TXT

tar -czf "$ARCHIVE_PATH" -C "$OUTPUT_DIR" "$PACKAGE_NAME"

cat <<EOF
[GraphRAG package] Created:
  $ARCHIVE_PATH

[GraphRAG package] Contents are staged at:
  $PACKAGE_DIR

Transfer the .tar.gz to the interview machine, extract it, then run:
  ./scripts/start-demo.sh
EOF
