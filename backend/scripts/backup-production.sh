#!/usr/bin/env bash
set -euo pipefail

umask 077

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required" >&2
  exit 1
fi
if ! command -v pg_dump >/dev/null 2>&1; then
  echo "pg_dump is required" >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_root="${1:-backups}"
backup_dir="${backup_root%/}/graphrag-${timestamp}"
mkdir -p "$backup_dir"

pg_dump \
  --format=custom \
  --no-owner \
  --no-privileges \
  --file="$backup_dir/database.dump" \
  "$DATABASE_URL"

python_bin="${PYTHON_BIN:-.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="python3"
fi
"$python_bin" -m scripts.backup_blobs --output "$backup_dir"

(
  cd "$backup_dir"
  shasum -a 256 database.dump blob-manifest.json > SHA256SUMS
)

echo "$backup_dir"
