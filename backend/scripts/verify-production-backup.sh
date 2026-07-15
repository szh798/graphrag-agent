#!/usr/bin/env bash
set -euo pipefail

backup_dir="${1:?usage: verify-production-backup.sh BACKUP_DIRECTORY}"

if ! command -v pg_restore >/dev/null 2>&1; then
  echo "pg_restore is required" >&2
  exit 1
fi

(
  cd "$backup_dir"
  shasum -a 256 --check SHA256SUMS
  pg_restore --list database.dump >/dev/null
)

python3 - "$backup_dir/blob-manifest.json" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
root = manifest_path.parent
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for entry in manifest.get("entries", []):
    path = root / entry["path"]
    if not path.is_file():
        raise SystemExit(f"missing blob backup: {entry['path']}")
    if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
        raise SystemExit(f"blob checksum mismatch: {entry['path']}")
print(f"verified database dump and {manifest.get('count', 0)} blobs")
PY
