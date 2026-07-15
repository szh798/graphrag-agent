"""Export every Blob referenced by documents and indexing job artifacts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from storage import app_repository as app_store
from storage import blob_repository as blob_store


def _safe_relative_path(key: str, fallback: str) -> Path:
    cleaned = "/".join(part for part in key.replace("\\", "/").split("/") if part not in {"", ".", ".."})
    return Path(cleaned or fallback)


def _referenced_blobs() -> list[dict]:
    app_repo = app_store.get_app_repository()
    refs: list[dict] = []
    for document in app_repo.list_documents():
        ref = document.get("blob_ref") or document.get("blob")
        if not ref and document.get("blob_key"):
            ref = {
                "key": document["blob_key"],
                "url": document.get("blob_url"),
                "download_url": document.get("blob_download_url"),
            }
        if isinstance(ref, dict):
            refs.append(ref)
    for job in app_repo.list_all_jobs():
        for ref in (job.get("artifacts") or {}).values():
            if isinstance(ref, dict):
                refs.append(ref)

    unique: dict[str, dict] = {}
    for ref in refs:
        identity = str(ref.get("download_url") or ref.get("downloadUrl") or ref.get("url") or ref.get("key") or "")
        if identity:
            unique.setdefault(identity, ref)
    return list(unique.values())


def export_blobs(output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    blob_dir = output_dir / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    repository = blob_store.get_blob_repository()
    entries: list[dict] = []

    for index, ref in enumerate(_referenced_blobs(), start=1):
        key = str(ref.get("key") or ref.get("pathname") or f"blob-{index}")
        target = blob_dir / _safe_relative_path(key, f"blob-{index}")
        downloaded = repository.download_to_path(ref, target)
        digest = hashlib.sha256(downloaded.read_bytes()).hexdigest()
        entries.append({
            "key": key,
            "path": str(downloaded.relative_to(output_dir)),
            "size_bytes": downloaded.stat().st_size,
            "sha256": digest,
        })

    manifest = {
        "backend": repository.profile().get("backend"),
        "count": len(entries),
        "entries": entries,
    }
    (output_dir / "blob-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = export_blobs(args.output)
    print(json.dumps({"count": manifest["count"], "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o077)
    main()
