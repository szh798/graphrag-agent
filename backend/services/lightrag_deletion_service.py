"""Durable, idempotent deletion of LightRAG document data."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from operations import report_event
from lightrag_integration.errors import LightRAGUnavailableError
from services import async_bridge
from storage import app_repository as app_store
from storage import queue_repository as queue_store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _delete_payload(doc: dict) -> dict:
    indexes = doc.get("indexes") if isinstance(doc.get("indexes"), dict) else {}
    stats = (indexes.get("lightrag") or {}).get("stats") or {}
    scope = str(doc.get("lightrag_workspace_scope") or "").strip().lower()
    tenant_id = str(doc.get("lightrag_tenant_id") or "").strip()
    if not tenant_id:
        tenant_id = "public_demo" if scope == "public_demo" else str(doc.get("owner_id") or "default")
    return {
        "tenant_id": tenant_id,
        "doc_id": str(doc.get("doc_id") or ""),
        "page_count": int(doc.get("pages") or stats.get("pages") or 0) or None,
        "page_ids": list(stats.get("page_ids") or []),
    }


def _require_complete_delete(result: object) -> dict:
    """Reject partial/ambiguous deletion so the durable job remains retryable."""

    if not isinstance(result, dict):
        raise LightRAGUnavailableError("LightRAG deletion returned an invalid result")
    failed_page_ids = [
        str(item)
        for item in (result.get("failed_page_ids") or [])
        if str(item)
    ]
    if result.get("deleted") is not True or failed_page_ids:
        raise LightRAGUnavailableError("LightRAG deletion is incomplete and must be retried")
    return result


def run_delete_job(job_id: str) -> dict | None:
    repo = app_store.get_app_repository()
    meta = repo.load_job_meta(job_id)
    if not meta or meta.get("job_type") != "lightrag_delete":
        return meta
    payload = dict(meta.get("delete_payload") or {})
    meta.update({"status": "running", "stage": "Deleting LightRAG data", "updated_at": _now()})
    repo.save_job_meta(job_id, meta)
    try:
        from services import lightrag_service

        result = async_bridge.run(lightrag_service.delete_document(
            tenant_id=payload["tenant_id"],
            doc_id=payload["doc_id"],
            page_count=payload.get("page_count"),
            page_ids=payload.get("page_ids") or None,
        ))
        result = _require_complete_delete(result)
        meta.update({
            "status": "done",
            "stage": "LightRAG deletion complete",
            "result": result,
            "error": None,
            "updated_at": _now(),
        })
        repo.save_job_meta(job_id, meta)
        return meta
    except Exception as exc:
        meta.update({"status": "queued", "stage": "LightRAG deletion queued for retry", "error": str(exc), "updated_at": _now()})
        repo.save_job_meta(job_id, meta)
        report_event(
            "lightrag_delete_retry_queued",
            "LightRAG document deletion failed and remains recoverable",
            severity="error",
            source="lightrag_delete_worker",
            context={"job_id": job_id, "doc_id": payload.get("doc_id"), "error": str(exc)},
        )
        raise


def delete_or_schedule(doc: dict) -> dict:
    payload = _delete_payload(doc)
    if not payload["doc_id"]:
        return {"status": "skipped"}
    indexes = doc.get("indexes") if isinstance(doc.get("indexes"), dict) else {}
    lightrag_state = (indexes.get("lightrag") or {}).get("status")
    if lightrag_state not in {"done", "indexing", "failed"}:
        return {"status": "not_indexed"}

    job_id = f"delete_{uuid.uuid4().hex[:10]}"
    now = _now()
    meta = {
        "job_id": job_id,
        "job_type": "lightrag_delete",
        # Keep the tombstone outside ordinary document-job cleanup. The source
        # row may be deleted immediately while this retry record must survive.
        "doc_id": f"__lightrag_delete__:{payload['doc_id']}",
        "source_doc_id": payload["doc_id"],
        "owner_id": payload["tenant_id"],
        "status": "queued",
        "stage": "Queued LightRAG deletion",
        "created_at": now,
        "updated_at": now,
        "progress": {},
        "delete_payload": payload,
        "error": None,
    }
    repo = app_store.get_app_repository()
    repo.save_job_meta(job_id, meta)
    queue_repo = queue_store.get_queue_repository()
    if getattr(queue_repo, "is_durable", lambda: False)():
        queue_repo.enqueue_index_job({"job_id": job_id, "job_type": "lightrag_delete"})
    else:
        threading.Thread(target=lambda: _run_delete_safely(job_id), daemon=True).start()
    return {"status": "queued", "job_id": job_id}


def _run_delete_safely(job_id: str) -> None:
    try:
        run_delete_job(job_id)
    except Exception:
        # The durable metadata is intentionally retained for a later retry.
        return
