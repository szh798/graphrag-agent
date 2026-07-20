"""Document Service — file upload, metadata CRUD."""
from __future__ import annotations

import os
import hashlib
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from storage import app_repository as app_store
from storage import blob_repository as blob_store
from storage import file_store as fs
from storage import graph_repository as graph_store

ALLOWED_EXTENSIONS = {
    "pdf",
    "docx",
    "doc",
    "pptx",
    "ppt",
    "png",
    "jpg",
    "jpeg",
    "html",
    "txt",
    "md",
    "markdown",
}
INTRINSIC_SINGLE_PAGE_EXTENSIONS = {"png", "jpg", "jpeg", "txt", "md", "markdown"}
MAX_FILE_SIZE_MB = 200
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
_INDEXING_DOCUMENT_STATUSES = {"submitted", "queued", "parsing", "extracting", "indexing"}
_document_update_lock = threading.RLock()

_GENERIC_MIME_TYPES = {"", "application/octet-stream", "binary/octet-stream"}
_ALLOWED_MIME_TYPES = {
    "pdf": {"application/pdf", "application/x-pdf"},
    "doc": {"application/msword", "application/vnd.ms-office", "application/x-ole-storage"},
    "docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/x-zip-compressed",
    },
    "ppt": {"application/vnd.ms-powerpoint", "application/vnd.ms-office", "application/x-ole-storage"},
    "pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip",
        "application/x-zip-compressed",
    },
    "png": {"image/png"},
    "jpg": {"image/jpeg", "image/jpg"},
    "jpeg": {"image/jpeg", "image/jpg"},
    "html": {"text/html", "application/xhtml+xml"},
    "txt": {"text/plain"},
    "md": {"text/plain", "text/markdown", "text/x-markdown"},
    "markdown": {"text/plain", "text/markdown", "text/x-markdown"},
}
_ZIP_PREFIXES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_PREFIX = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_supported_image_format(head: bytes) -> tuple[str, str] | None:
    """Return the real supported image extension and MIME from its signature."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    return None


def normalize_document_status(status: object) -> str:
    """Map internal job states onto the stable public document contract."""
    value = str(status or "").strip().lower()
    if value in _INDEXING_DOCUMENT_STATUSES:
        return "indexing"
    if value in {"done", "indexed"}:
        return "indexed"
    if value in {"cancelled", "uploaded"}:
        return "uploaded"
    if value == "failed":
        return "failed"
    return "unknown"


def _engine_status_from_document(status: object) -> str:
    value = normalize_document_status(status)
    return {
        "indexed": "done",
        "indexing": "indexing",
        "failed": "failed",
        "uploaded": "pending",
    }.get(value, "pending")


def _initial_indexes() -> dict[str, dict]:
    lightrag_enabled = os.getenv("LIGHTRAG_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    return {
        "legacy": {"status": "pending", "job_id": None, "error": None, "stats": {}},
        "lightrag": {
            "status": "pending" if lightrag_enabled else "disabled",
            "job_id": None,
            "error": None,
            "stats": {},
        },
    }


def normalized_indexes(doc: dict) -> dict[str, dict]:
    """Return the dual-index state, repairing rows created before dual engines."""
    raw = doc.get("indexes") if isinstance(doc.get("indexes"), dict) else {}
    defaults = _initial_indexes()
    indexes: dict[str, dict] = {}
    for engine in ("legacy", "lightrag"):
        value = raw.get(engine) if isinstance(raw.get(engine), dict) else {}
        indexes[engine] = {**defaults[engine], **value}

    # The top-level status remains the legacy compatibility contract and is
    # authoritative for records that predate per-engine metadata.
    if not isinstance(raw.get("legacy"), dict):
        indexes["legacy"]["status"] = _engine_status_from_document(doc.get("status"))
    return indexes


def lightrag_tenant_for_document(doc: dict, *, persist: bool = False) -> str:
    """Return the immutable logical LightRAG scope for a document.

    Older rows are classified once from PUBLIC_DOCUMENT_IDS. Newer rows keep
    ``lightrag_workspace_scope`` so later environment changes or account-wide
    deletion cannot target the wrong workspace.
    """

    scope = str(doc.get("lightrag_workspace_scope") or "").strip().lower()
    if scope == "public_demo":
        return "public_demo"
    if scope == "owner":
        return str(doc.get("owner_id") or "default")

    doc_id = str(doc.get("doc_id") or "")
    public_ids = {
        item.strip()
        for item in os.getenv("PUBLIC_DOCUMENT_IDS", "").split(",")
        if item.strip()
    }
    scope = "public_demo" if doc_id in public_ids else "owner"
    tenant_id = "public_demo" if scope == "public_demo" else str(doc.get("owner_id") or "default")
    if persist and doc_id:
        with _document_update_lock:
            repo = app_store.get_app_repository()
            current = repo.get_document(doc_id)
            if current and not current.get("lightrag_workspace_scope"):
                current["lightrag_workspace_scope"] = scope
                repo.save_document(current)
                doc["lightrag_workspace_scope"] = scope
    return tenant_id


def update_engine_index_status(
    doc_id: str,
    engine: str,
    status: str,
    *,
    job_id: str | None = None,
    error: str | None = None,
    stats: dict | None = None,
    pages: int | None = None,
) -> dict | None:
    if engine not in {"legacy", "lightrag"}:
        raise ValueError(f"Unsupported engine: {engine}")
    with _document_update_lock:
        repo = app_store.get_app_repository()
        get_document = getattr(repo, "get_document", None)
        if not callable(get_document):
            return None
        doc = get_document(doc_id)
        if not doc:
            return None
        indexes = normalized_indexes(doc)
        state = dict(indexes[engine])
        state["status"] = status
        if job_id is not None:
            state["job_id"] = job_id
        state["error"] = error
        if stats is not None:
            state["stats"] = stats
        indexes[engine] = state
        doc["indexes"] = indexes
        if pages is not None:
            doc["pages"] = pages
        if engine == "legacy":
            doc["status"] = {
                "done": "indexed",
                "failed": "failed",
                "cancelled": "uploaded",
                "pending": "uploaded",
            }.get(status, "indexing")
        save_document = getattr(repo, "save_document", None)
        if callable(save_document):
            save_document(doc)
        elif engine == "legacy":
            update_status = getattr(repo, "update_document_status", None)
            if callable(update_status):
                update_status(doc_id, doc["status"], pages)
        return doc


def public_document(doc: dict) -> dict:
    """Return a frontend/API-safe document payload."""
    item = dict(doc)
    if "status" in item or "indexes" in item:
        item["indexes"] = normalized_indexes(item)
        item["available_engines"] = [
            engine for engine in ("legacy", "lightrag")
            if item["indexes"].get(engine, {}).get("status") == "done"
        ]
    if "status" in item:
        item["status"] = normalize_document_status(item.get("status"))
    file_format = str(item.get("format") or Path(str(item.get("filename") or "")).suffix.lstrip(".")).lower()
    if item.get("pages") is None and file_format in INTRINSIC_SINGLE_PAGE_EXTENSIONS:
        # Text/Markdown and image uploads are represented as one logical page
        # by the local parser. This also repairs legacy rows created before the
        # page count was persisted at upload time.
        item["pages"] = 1
    uploaded_at = item.get("uploaded_at") or item.get("upload_date")
    if uploaded_at:
        item["uploaded_at"] = uploaded_at
        item["upload_date"] = uploaded_at
    for internal_key in (
        "upload_filename",
        "blob_key",
        "blob_url",
        "blob_ref",
        "owner_id",
        "actor_id",
        "lightrag_workspace_scope",
    ):
        item.pop(internal_key, None)
    return item


def validate_upload(filename: str, size_bytes: int) -> tuple[bool, int, str]:
    """Returns (ok, error_code, error_msg)."""
    if not filename or "/" in filename or "\\" in filename:
        return False, 1001, "Invalid filename"
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXTENSIONS:
        return False, 1002, f"Unsupported file format: .{ext}. Supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
    size_mb = size_bytes / (1024 * 1024)
    if size_bytes > MAX_FILE_SIZE_BYTES:
        return False, 1003, f"File size {size_mb:.1f}MB exceeds {MAX_FILE_SIZE_MB}MB limit"
    return True, 0, ""


def validate_upload_content(
    filename: str,
    content_type: str | None,
    head: bytes,
    size_bytes: int,
) -> tuple[bool, int, str]:
    """Validate declared MIME type and lightweight file signatures.

    Generic or absent MIME types remain accepted for CLI/offline clients; the
    file signature still has to match formats with a stable magic value.
    """
    if size_bytes <= 0:
        return False, 1001, "File is empty"

    ext = Path(filename).suffix.lower().lstrip(".")
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    if mime not in _GENERIC_MIME_TYPES and mime not in _ALLOWED_MIME_TYPES.get(ext, set()):
        return False, 1002, f"File content type does not match .{ext} format"

    signature_ok = True
    if ext == "pdf":
        signature_ok = b"%PDF-" in head[:1024]
    elif ext == "png":
        signature_ok = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif ext in {"jpg", "jpeg"}:
        signature_ok = head.startswith(b"\xff\xd8\xff")
    elif ext in {"docx", "pptx"}:
        signature_ok = head.startswith(_ZIP_PREFIXES)
    elif ext in {"doc", "ppt"}:
        signature_ok = head.startswith(_OLE_PREFIX) or (ext == "doc" and head.startswith(b"{\\rtf"))
    elif ext in {"html", "txt", "md", "markdown"}:
        signature_ok = b"\x00" not in head

    if not signature_ok:
        return False, 1002, f"File content does not match .{ext} format"
    return True, 0, ""


def save_upload(filename: str, content: bytes, language: str = "ch",
                enable_formula: bool = True, enable_table: bool = True,
                owner_id: str = "default", actor_id: str | None = None) -> dict:
    doc_id = uuid.uuid4().hex[:8]
    upload_filename = f"{doc_id}_{filename}"
    blob_ref = blob_store.get_blob_repository().save_upload(upload_filename, content)
    return _save_document_record(
        doc_id=doc_id,
        filename=filename,
        size_bytes=len(content),
        language=language,
        enable_formula=enable_formula,
        enable_table=enable_table,
        upload_filename=upload_filename,
        blob_ref=blob_ref,
        owner_id=owner_id,
        actor_id=actor_id,
    )


def _save_document_record(
    *,
    doc_id: str,
    filename: str,
    size_bytes: int,
    language: str,
    enable_formula: bool,
    enable_table: bool,
    upload_filename: str,
    blob_ref: dict,
    content_type: str | None = None,
    owner_id: str = "default",
    actor_id: str | None = None,
) -> dict:
    ext = Path(filename).suffix.lower().lstrip(".")

    uploaded_at = datetime.now(timezone.utc).isoformat()
    doc = {
        "doc_id": doc_id,
        "filename": filename,
        "format": ext,
        "size_bytes": size_bytes,
        "pages": 1 if ext in INTRINSIC_SINGLE_PAGE_EXTENSIONS else None,
        "uploaded_at": uploaded_at,
        "upload_date": uploaded_at,
        "status": "uploaded",
        "language": language,
        "enable_formula": enable_formula,
        "enable_table": enable_table,
        "upload_filename": upload_filename,  # internal: actual stored filename
        "blob_key": blob_ref.get("key") or blob_ref.get("pathname") or upload_filename,
        "blob_url": blob_ref.get("url", ""),
        "blob_ref": blob_ref,
        "content_type": content_type or blob_ref.get("contentType") or blob_ref.get("content_type"),
        "owner_id": owner_id,
        "indexes": _initial_indexes(),
    }
    if actor_id:
        doc["actor_id"] = actor_id
    app_store.get_app_repository().save_document(doc)
    return doc


def register_direct_upload(
    filename: str,
    size_bytes: int,
    content_type: str | None,
    blob_ref: dict,
    language: str = "ch",
    enable_formula: bool = True,
    enable_table: bool = True,
    owner_id: str = "default",
    actor_id: str | None = None,
) -> dict:
    """Register a browser-to-Blob upload after Vercel confirms completion."""
    ok, code, message = validate_upload(filename, size_bytes)
    if not ok:
        raise ValueError(f"{code}:{message}")

    mime = (content_type or "").split(";", 1)[0].strip().lower()
    ext = Path(filename).suffix.lower().lstrip(".")
    if mime not in _GENERIC_MIME_TYPES and mime not in _ALLOWED_MIME_TYPES.get(ext, set()):
        raise ValueError("1002:File content type does not match filename")

    url = str(blob_ref.get("url") or "")
    download_url = str(blob_ref.get("downloadUrl") or blob_ref.get("download_url") or "")
    pathname = str(blob_ref.get("pathname") or "")
    parsed = urlparse(url or download_url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".blob.vercel-storage.com"):
        raise ValueError("1001:Invalid Blob storage URL")
    if not pathname.startswith("uploads/") or ".." in Path(pathname).parts:
        raise ValueError("1001:Invalid Blob pathname")

    normalized_blob = dict(blob_ref)
    normalized_blob["key"] = pathname
    normalized_blob["download_url"] = download_url
    normalized_blob["content_type"] = mime
    repo = app_store.get_app_repository()
    find_existing = getattr(repo, "find_document_by_blob", None)
    if callable(find_existing):
        existing = find_existing(owner_id, pathname)
        if existing:
            return existing

    # Vercel may redeliver the completion callback. A deterministic ID makes
    # concurrent deliveries converge on the same business row as well.
    doc_id = hashlib.sha256(
        f"{owner_id}\x00{pathname}".encode("utf-8")
    ).hexdigest()[:16]
    return _save_document_record(
        doc_id=doc_id,
        filename=filename,
        size_bytes=size_bytes,
        language=language,
        enable_formula=enable_formula,
        enable_table=enable_table,
        upload_filename=pathname,
        blob_ref=normalized_blob,
        content_type=mime,
        owner_id=owner_id,
        actor_id=actor_id,
    )


def get_document(doc_id: str) -> dict | None:
    return app_store.get_app_repository().get_document(doc_id)


def list_documents(page: int = 1, page_size: int = 20,
                   status: str | None = None, fmt: str | None = None,
                   allowed_ids: set[str] | None = None) -> dict:
    app_repo = app_store.get_app_repository()
    items = app_repo.list_documents()
    items.sort(key=lambda d: d.get("uploaded_at", ""), reverse=True)
    if allowed_ids is not None:
        items = [d for d in items if d.get("doc_id") in allowed_ids]
    if status:
        items = [d for d in items if normalize_document_status(d.get("status")) == status]
    if fmt:
        items = [d for d in items if d.get("format") == fmt.lower()]
    total = len(items)
    start = (page - 1) * page_size
    latest_jobs: dict[str, dict] = {}
    for meta in app_repo.list_all_jobs():
        doc_id = str(meta.get("doc_id") or "")
        if not doc_id:
            continue
        current = latest_jobs.get(doc_id)
        if current is None or str(meta.get("created_at") or "") > str(current.get("created_at") or ""):
            latest_jobs[doc_id] = meta

    public_items: list[dict] = []
    for item in items[start: start + page_size]:
        public_item = public_document(item)
        latest_job = latest_jobs.get(str(item.get("doc_id") or ""))
        if latest_job:
            job_status = str(latest_job.get("status") or "").strip().lower()
            if job_status in _INDEXING_DOCUMENT_STATUSES:
                public_item["job_id"] = latest_job.get("job_id")
                public_item["index_job_status"] = job_status
                public_item["index_stage"] = latest_job.get("stage")
                public_item["progress"] = latest_job.get("progress")
        if latest_job and public_item.get("status") == "failed" and latest_job.get("status") == "failed":
            public_item["error_msg"] = latest_job.get("error") or latest_job.get("stage")
        public_items.append(public_item)

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": public_items,
    }


def delete_document(doc_id: str) -> tuple[bool, int, int]:
    """Delete doc and its KG contributions. Returns (ok, removed_nodes, removed_edges)."""
    app_repo = app_store.get_app_repository()
    doc = app_repo.get_document(doc_id)
    if not doc:
        return False, 0, 0

    # Cancel before removing any artifact or application row. Detached
    # cancellation metadata intentionally survives document/tenant cleanup so
    # a queued or already-running Worker cannot publish stale results later.
    from services import indexing_service

    cancelled_job_ids = set(indexing_service.cancel_document_jobs(doc_id, detach=True))
    indexing_service.purge_cancelled_job_artifacts(cancelled_job_ids)

    # LightRAG deletion is idempotent and durable. It is scheduled before the
    # application row is removed so all workspace/page identifiers are retained.
    from services import lightrag_deletion_service

    doc_for_delete = {**doc, "indexes": normalized_indexes(doc)}
    doc_for_delete["lightrag_tenant_id"] = lightrag_tenant_for_document(doc)
    lightrag_deletion_service.delete_or_schedule(doc_for_delete)

    # Remove from KG
    removed_nodes, removed_edges = graph_store.get_graph_repository().remove_document(doc_id)

    # Remove upload file
    blob_repo = blob_store.get_blob_repository()
    if doc.get("blob_ref") or doc.get("blob_key"):
        blob_repo.delete(doc.get("blob_ref") or doc.get("blob_key"))
    else:
        upload_filename = doc.get("upload_filename", "")
        upload_path = fs.UPLOADS_DIR / upload_filename
        if upload_path.exists():
            upload_path.unlink(missing_ok=True)

    # Remove associated jobs
    for meta in app_repo.list_all_jobs():
        if str(meta.get("job_id") or "") in cancelled_job_ids:
            continue
        if meta.get("doc_id") == doc_id:
            for artifact in (meta.get("artifacts") or {}).values():
                if isinstance(artifact, dict):
                    blob_repo.delete(artifact)
            fs.delete_job(meta["job_id"])
            app_repo.delete_job(meta["job_id"])

    # Remove from index
    app_repo.delete_document(doc_id)

    return True, removed_nodes, removed_edges


def update_doc_status(doc_id: str, status: str, pages: int | None = None) -> None:
    app_store.get_app_repository().update_document_status(doc_id, status, pages)


def _latest_done_job(doc_id: str) -> dict | None:
    jobs: list[dict] = []
    for meta in app_store.get_app_repository().list_all_jobs():
        if meta.get("doc_id") != doc_id or meta.get("status") not in {"done", "partial"}:
            continue
        targets = meta.get("target_engines")
        # Pre-dual jobs did not persist target_engines and were classic-only.
        if targets is not None and "legacy" not in set(targets):
            continue
        legacy = (meta.get("engines") or {}).get("legacy")
        if isinstance(legacy, dict) and legacy.get("status") != "done":
            continue
        jobs.append(meta)
    if not jobs:
        return None
    return sorted(jobs, key=lambda meta: meta.get("created_at", ""), reverse=True)[0]


def get_document_index_result(doc_id: str) -> dict | None:
    document = app_store.get_app_repository().get_document(doc_id)
    if not document:
        return None
    meta = _latest_done_job(doc_id)
    if not meta:
        # Legacy indexed documents can outlive their transient job metadata and
        # artifacts.  The persisted graph is still authoritative for the
        # durable node/edge counts, so expose an honest partial result instead
        # of reporting that the index result does not exist.
        if document.get("status") != "indexed":
            return None
        try:
            graph = graph_store.get_graph_repository().export_kg(doc_id)
        except Exception:
            return None

        nodes = graph.get("nodes") or []
        edges = graph.get("edges") or []
        if not nodes and not edges:
            return None

        public_doc = public_document(document)
        pages = int(public_doc.get("pages") or 0)
        summary = {
            "nodes": int(graph.get("total_nodes") or len(nodes)),
            "edges": int(graph.get("total_edges") or len(edges)),
            "pages": pages,
        }
        return {
            "job_id": f"recovered-{doc_id}",
            "doc_id": doc_id,
            "status": "done",
            "stage": "Recovered from persisted graph",
            "created_at": document.get("uploaded_at") or document.get("upload_date"),
            "summary": summary,
            "stats": summary,
            "nodes": [],
            "edges": [],
            "extractions": [],
            "recovered": True,
        }

    job_id = meta["job_id"]
    job_dir = fs.job_dir(job_id)
    blob_repo = blob_store.get_blob_repository()

    def _artifact(name: str, default):
        artifact = (meta.get("artifacts") or {}).get(name)
        if artifact:
            value = blob_repo.read_json(artifact.get("key") or artifact.get("pathname") or "")
            if value is not None:
                return value
        return fs.read_json(job_dir / name) or default

    stats = _artifact("stats.json", {})
    extractions = _artifact("extractions.json", [])
    nodes = _artifact("kg_nodes.json", [])
    edges = _artifact("kg_edges.json", [])

    summary = {
        "nodes": int(stats.get("nodes") or len(nodes)),
        "edges": int(stats.get("edges") or len(edges)),
        "pages": int(stats.get("pages") or 0),
        "extractions": int(stats.get("raw_extractions") or len(extractions)),
        "duration_seconds": float(stats.get("elapsed_seconds") or meta.get("elapsed_seconds") or 0),
    }

    return {
        "job_id": job_id,
        "doc_id": doc_id,
        "status": meta.get("status"),
        "stage": meta.get("stage", ""),
        "created_at": meta.get("created_at"),
        "elapsed_seconds": meta.get("elapsed_seconds", summary["duration_seconds"]),
        "summary": summary,
        "stats": stats,
        "nodes": nodes,
        "edges": edges,
        "extractions": extractions,
    }


def get_document_extractions(doc_id: str, page: int = 1, page_size: int = 50) -> dict | None:
    result = get_document_index_result(doc_id)
    if not result:
        return None

    page_size = min(max(page_size, 1), 200)
    page = max(page, 1)
    records = result.get("extractions") or []
    start = (page - 1) * page_size
    return {
        "doc_id": doc_id,
        "job_id": result["job_id"],
        "total": len(records),
        "page": page,
        "page_size": page_size,
        "items": records[start: start + page_size],
        "summary": result["summary"],
    }
