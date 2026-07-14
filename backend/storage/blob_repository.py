"""Blob/object storage repository backends."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from storage import file_store as fs


class FileBlobRepository:
    def profile(self) -> dict:
        return {
            "backend": "filesystem",
            "uploads_dir": str(fs.UPLOADS_DIR),
            **fs.storage_profile(),
        }

    def health(self) -> dict:
        return {
            "status": "ok",
            "backend": "filesystem",
            "uploads_dir_exists": fs.UPLOADS_DIR.exists(),
            **fs.storage_profile(),
        }

    def save_upload(self, key: str, content: bytes, content_type: str | None = None) -> dict:
        path = fs.UPLOADS_DIR / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"key": key, "pathname": key, "path": str(path), "url": str(path), "size_bytes": len(content)}

    def save_bytes(self, key: str, content: bytes, content_type: str | None = None) -> dict:
        path = fs._BASE / "artifacts" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return {"key": key, "pathname": key, "path": str(path), "url": str(path), "size_bytes": len(content)}

    def save_json(self, key: str, data: Any) -> dict:
        return self.save_bytes(key, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")

    def read_json(self, key: str) -> Any:
        for path in (fs._BASE / "artifacts" / key, fs.UPLOADS_DIR / key, Path(key)):
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def download_to_path(self, blob_ref: dict, target_path: Path) -> Path:
        source = Path(blob_ref.get("path") or blob_ref.get("url") or blob_ref.get("key") or "")
        if not source.is_absolute():
            source = fs.UPLOADS_DIR / source
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target_path)
        return target_path

    def delete(self, blob_ref: dict | str) -> None:
        key = blob_ref if isinstance(blob_ref, str) else blob_ref.get("path") or blob_ref.get("key") or ""
        for path in (Path(key), fs.UPLOADS_DIR / key, fs._BASE / "artifacts" / key):
            if path.exists():
                path.unlink(missing_ok=True)


class VercelBlobRepository:
    def __init__(self):
        self.token = os.getenv("BLOB_READ_WRITE_TOKEN", "").strip()

    def profile(self) -> dict:
        return {
            "backend": "vercel_blob",
            "token_configured": bool(self.token),
        }

    def health(self) -> dict:
        if not self.token:
            return {"status": "error", **self.profile(), "error": "BLOB_READ_WRITE_TOKEN is not configured"}
        try:
            import vercel_blob  # noqa: F401
        except ImportError:
            return {"status": "error", **self.profile(), "error": "Install vercel-blob to use GRAPHRAG_BLOB_BACKEND=vercel_blob"}
        return {"status": "ok", **self.profile()}

    def _client(self):
        if not self.token:
            raise ValueError("BLOB_READ_WRITE_TOKEN is required when GRAPHRAG_BLOB_BACKEND=vercel_blob")
        try:
            import vercel_blob
        except ImportError as exc:
            raise RuntimeError("Install vercel-blob to use GRAPHRAG_BLOB_BACKEND=vercel_blob") from exc
        os.environ.setdefault("BLOB_READ_WRITE_TOKEN", self.token)
        return vercel_blob

    def save_upload(self, key: str, content: bytes, content_type: str | None = None) -> dict:
        return self.save_bytes(key, content, content_type)

    def save_bytes(self, key: str, content: bytes, content_type: str | None = None) -> dict:
        client = self._client()
        put = getattr(client, "put", None)
        if not callable(put):
            raise RuntimeError("vercel_blob.put is unavailable")
        result = put(key, content, {"access": os.getenv("BLOB_ACCESS", "private"), "contentType": content_type} if content_type else {"access": os.getenv("BLOB_ACCESS", "private")})
        return dict(result) if isinstance(result, dict) else {
            "key": key,
            "pathname": getattr(result, "pathname", key),
            "url": getattr(result, "url", ""),
            "download_url": getattr(result, "download_url", getattr(result, "downloadUrl", "")),
        }

    def save_json(self, key: str, data: Any) -> dict:
        return self.save_bytes(key, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), "application/json")

    def read_json(self, key: str) -> Any:
        data = self.read_bytes({"key": key, "url": key})
        return json.loads(data.decode("utf-8")) if data else None

    def read_bytes(self, blob_ref: dict) -> bytes:
        import requests

        url = blob_ref.get("download_url") or blob_ref.get("downloadUrl") or blob_ref.get("url") or blob_ref.get("key")
        if not url:
            return b""
        response = requests.get(url, headers={"Authorization": f"Bearer {self.token}"}, timeout=60)
        response.raise_for_status()
        return response.content

    def download_to_path(self, blob_ref: dict, target_path: Path) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(self.read_bytes(blob_ref))
        return target_path

    def delete(self, blob_ref: dict | str) -> None:
        client = self._client()
        delete = getattr(client, "delete", None) or getattr(client, "del_", None) or getattr(client, "del", None)
        if not callable(delete):
            raise RuntimeError("vercel_blob delete function is unavailable")
        value = blob_ref if isinstance(blob_ref, str) else blob_ref.get("url") or blob_ref.get("key")
        if value:
            delete(value)


_CACHE_KEY: tuple[str, str] | None = None
_CACHE_REPO: FileBlobRepository | VercelBlobRepository | None = None


def get_blob_repository() -> FileBlobRepository | VercelBlobRepository:
    global _CACHE_KEY, _CACHE_REPO
    backend = os.getenv("GRAPHRAG_BLOB_BACKEND", os.getenv("GRAPHRAG_STORAGE_BACKEND", "filesystem")).strip().lower()
    if backend in {"json", "file", "local"}:
        backend = "filesystem"
    key = (backend, os.getenv("BLOB_READ_WRITE_TOKEN", ""))
    if _CACHE_REPO is not None and _CACHE_KEY == key:
        return _CACHE_REPO
    _CACHE_KEY = key
    _CACHE_REPO = VercelBlobRepository() if backend in {"vercel_blob", "blob", "vercel"} else FileBlobRepository()
    return _CACHE_REPO


def reset_blob_repository_cache() -> None:
    global _CACHE_KEY, _CACHE_REPO
    _CACHE_KEY = None
    _CACHE_REPO = None
