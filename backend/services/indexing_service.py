"""Indexing Service — Pipeline orchestration (parsing → extracting → indexing)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from storage import app_repository as app_store
from storage import blob_repository as blob_store
from storage import file_store as fs
from services import async_bridge, document_service
from services.document_service import update_doc_status
from services.mineru_cloud_client import MinerUCloudClient
from services import local_parser
from storage import graph_repository as graph_store
from storage import queue_repository as queue_store
from pipeline.embeddings import embed_texts
from operations import report_event

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# In-memory registry of active jobs {job_id: threading.Thread}
_active_threads: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, bool] = {}
_meta_update_lock = threading.RLock()
_artifact_update_lock = threading.RLock()
_TERMINAL_JOB_STATUSES = {"done", "partial", "failed", "cancelled"}
_ACTIVE_JOB_STATUSES = {
    "submitted",
    "queued",
    "pending",
    "parsing",
    "extracting",
    "indexing",
    "running",
}


class _IndexCancelled(RuntimeError):
    pass


def _chunks_from_pages(doc_id: str, pages: list) -> list[dict]:
    chunks: list[dict] = []
    for page in pages:
        text = getattr(page, "text", "")
        if not text.strip():
            continue
        chunks.append({
            "chunk_id": f"{doc_id}:page:{getattr(page, 'page_idx', 0)}",
            "doc_id": doc_id,
            "page": int(getattr(page, "page_idx", 0)),
            "text": text,
        })
    return chunks


def _parser_mode() -> str:
    mode = os.getenv("PARSER_MODE", "auto").strip().lower()
    if mode in {"cloud", "mineru", "mineru_cloud"}:
        return "mineru"
    if mode in {"auto", "local"}:
        return mode
    return "auto"


def _should_use_mineru() -> bool:
    mode = _parser_mode()
    if mode == "mineru":
        return True
    if mode == "local":
        return False
    return bool(os.getenv("MINERU_API_TOKEN", "").strip())


def _should_use_mineru_for_document(doc: dict) -> bool:
    filename_extension = Path(str(doc.get("filename") or "")).suffix.lower().lstrip(".")
    extension = str(doc.get("format") or filename_extension).strip().lower().lstrip(".")
    if extension in {"html", "htm", "txt", "md", "markdown"}:
        return False
    return _should_use_mineru()


def _should_embed_graph() -> bool:
    mode = os.getenv("GRAPHRAG_ENABLE_EMBEDDINGS", "auto").strip().lower()
    if mode in {"1", "true", "yes", "on"}:
        return True
    if mode in {"0", "false", "no", "off"}:
        return False
    profile = graph_store.get_graph_repository().profile()
    return profile.get("backend") == "neo4j"


def _attach_embeddings(nodes: list[dict], chunks: list[dict]) -> None:
    if not _should_embed_graph():
        return
    texts = [f"{node.get('name', '')} {node.get('type', '')}".strip() for node in nodes]
    texts.extend(chunk.get("text", "") for chunk in chunks)
    try:
        vectors = embed_texts(texts)
    except Exception:
        if os.getenv("GRAPHRAG_REQUIRE_EMBEDDINGS", "false").lower() in {"1", "true", "yes", "on"}:
            raise
        return
    node_count = len(nodes)
    for node, vector in zip(nodes, vectors[:node_count]):
        node["embedding"] = vector
    for chunk, vector in zip(chunks, vectors[node_count:]):
        chunk["embedding"] = vector


def start_indexing(
    doc_id: str,
    engines: set[str] | None = None,
    *,
    idempotency_key: str | None = None,
) -> dict:
    app_repo = app_store.get_app_repository()
    doc = app_repo.get_document(doc_id)
    if not doc:
        return None  # type: ignore
    requested_engines = set(engines or {"legacy", "lightrag"})
    if not requested_engines or not requested_engines <= {"legacy", "lightrag"}:
        raise ValueError("engines must contain legacy and/or lightrag")

    stable_job_id = None
    if idempotency_key:
        digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()
        stable_job_id = f"job_auto_{digest[:20]}"
        existing = app_repo.load_job_meta(stable_job_id)
        if existing:
            return existing

    # Upload callbacks and a quick subsequent UI click may race. Reuse the
    # already-visible parent job instead of parsing/indexing the document twice.
    list_jobs = getattr(app_repo, "list_all_jobs", None)
    if callable(list_jobs):
        for existing in list_jobs():
            existing_targets = set(existing.get("target_engines") or ())
            if (
                existing.get("job_type") != "lightrag_delete"
                and str(existing.get("doc_id") or "") == str(doc_id)
                and existing.get("status") not in _TERMINAL_JOB_STATUSES
                and requested_engines <= existing_targets
            ):
                return existing

    job_id = stable_job_id or f"job_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    queue_repo = queue_store.get_queue_repository()
    queued = bool(getattr(queue_repo, "is_durable", lambda: False)())

    lightrag_enabled = os.getenv("LIGHTRAG_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    existing_indexes = document_service.normalized_indexes(doc)
    engine_meta: dict[str, dict] = {}
    for engine in ("legacy", "lightrag"):
        existing = dict(existing_indexes.get(engine) or {})
        if engine in requested_engines:
            status = "queued" if queued else "pending"
            if engine == "lightrag" and not lightrag_enabled:
                status = "disabled"
            engine_meta[engine] = {
                "status": status,
                "job_id": job_id,
                "error": None,
                "stats": existing.get("stats") or {},
            }
        else:
            engine_meta[engine] = existing

    meta = {
        "job_id": job_id,
        "doc_id": doc_id,
        "status": "queued" if queued else "submitted",
        "stage": "Queued for worker" if queued else "Job submitted",
        "progress": {"parsed_pages": 0, "total_pages": 0, "extracted_entities": 0},
        "created_at": now,
        "elapsed_seconds": 0.0,
        "error": None,
        "pdf_name": doc["filename"],
        "pdf_path": str(fs.UPLOADS_DIR / doc.get("upload_filename", "")),
        "blob_key": doc.get("blob_key"),
        "owner_id": str(doc.get("owner_id") or "default"),
        "actor_id": doc.get("actor_id"),
        "artifacts": {},
        "engines": engine_meta,
        "target_engines": sorted(requested_engines),
        "idempotent_upload": bool(stable_job_id),
    }
    app_repo.save_job_meta(job_id, meta)
    for engine in requested_engines:
        status = "indexing" if queued else "pending"
        if engine == "lightrag" and not lightrag_enabled:
            status = "disabled"
        document_service.update_engine_index_status(doc_id, engine, status, job_id=job_id)

    if queued:
        try:
            queue_repo.enqueue_index_job({"job_id": job_id, "doc_id": doc_id})
        except Exception as exc:
            # A persisted "queued" row without a Redis receipt would make the
            # idempotency check return an orphan forever. Mark it terminal so a
            # user retry creates a fresh parent and a fresh queue message.
            message = "Index job could not be added to the durable queue"
            _update_meta(
                job_id,
                status="failed",
                stage=f"Error: {message}",
                error=message,
            )
            for engine in requested_engines:
                state = engine_meta.get(engine) or {}
                if state.get("status") == "disabled":
                    continue
                _update_engine_meta(job_id, engine, status="failed", error=message)
                document_service.update_engine_index_status(
                    doc_id,
                    engine,
                    "failed",
                    job_id=job_id,
                    error=message,
                )
            report_event(
                "index_queue_enqueue_failed",
                message,
                severity="error",
                source="index_gateway",
                context={"job_id": job_id, "doc_id": doc_id, "error": type(exc).__name__},
            )
            raise
        return meta

    _cancel_flags[job_id] = False
    thread = threading.Thread(
        target=_run_local_job_serialized,
        args=(job_id,),
        daemon=True,
    )
    _active_threads[job_id] = thread
    thread.start()

    return meta


def _owner_lock(repo, owner_id: str, job_id: str) -> bool:
    acquire = getattr(repo, "acquire_index_owner_lock", None)
    return bool(acquire(owner_id, job_id)) if callable(acquire) else True


def _release_owner_lock(repo, owner_id: str, job_id: str) -> None:
    release = getattr(repo, "release_index_owner_lock", None)
    if callable(release):
        release(owner_id, job_id)


def _owner_lock_heartbeat(
    repo,
    owner_id: str,
    job_id: str,
    payload: dict | None = None,
) -> tuple[threading.Event, threading.Thread | None]:
    refresh = getattr(repo, "refresh_index_owner_lock", None)
    refresh_receipt = getattr(repo, "refresh_index_job_lease", None)
    stop = threading.Event()
    if not callable(refresh) and not (payload and callable(refresh_receipt)):
        return stop, None

    def heartbeat() -> None:
        # Upstash locks expire after worker crashes; healthy long-running jobs
        # renew them so a 200MB document cannot overlap with a sibling job.
        while not stop.wait(60):
            try:
                if callable(refresh) and not refresh(owner_id, job_id):
                    _cancel_flags[job_id] = True
                    report_event(
                        "index_owner_lock_lost",
                        "The per-tenant indexing lock could not be renewed",
                        severity="error",
                        source="index_worker",
                        context={"job_id": job_id},
                    )
                    return
                if payload and callable(refresh_receipt) and not refresh_receipt(payload):
                    # A recovery may already have copied this exact receipt
                    # back to the queue. The owner lock plus terminal-state
                    # check fences that copy from writing a second time.
                    report_event(
                        "index_queue_lease_lost",
                        "The indexing queue processing lease could not be renewed",
                        severity="error",
                        source="index_worker",
                        context={"job_id": job_id},
                    )
                    return
            except Exception as exc:
                # Ownership can no longer be proven. Stop at the next pipeline
                # checkpoint instead of risking overlap after the Redis TTL.
                _cancel_flags[job_id] = True
                report_event(
                    "index_owner_lock_refresh_failed",
                    "The per-tenant indexing lock refresh failed",
                    severity="warning",
                    source="index_worker",
                    context={"job_id": job_id, "error": type(exc).__name__},
                )
                return

    thread = threading.Thread(
        target=heartbeat,
        name=f"index-owner-lock-{job_id}",
        daemon=True,
    )
    thread.start()
    return stop, thread


def _run_local_job_serialized(job_id: str) -> None:
    meta = _load_job_meta(job_id)
    if not meta:
        return
    owner_id = str(meta.get("owner_id") or "default")
    repo = queue_store.get_queue_repository()
    acquired = False
    while not acquired:
        if _is_cancelled(job_id):
            _update_meta(job_id, status="cancelled", stage="Cancelled")
            _active_threads.pop(job_id, None)
            return
        acquired = _owner_lock(repo, owner_id, job_id)
        if not acquired:
            _update_meta(
                job_id,
                status="queued",
                stage="Waiting for this tenant's current indexing task",
            )
            time.sleep(0.25)
    stop, heartbeat = _owner_lock_heartbeat(repo, owner_id, job_id)
    try:
        _run_pipeline(job_id)
    finally:
        stop.set()
        if heartbeat:
            heartbeat.join(timeout=1)
        _release_owner_lock(repo, owner_id, job_id)


def _update_meta(job_id: str, **kwargs) -> None:
    with _meta_update_lock:
        app_repo = app_store.get_app_repository()
        meta = app_repo.load_job_meta(job_id) or {}
        meta.update(kwargs)
        meta["elapsed_seconds"] = round(
            (datetime.now(timezone.utc) - datetime.fromisoformat(meta["created_at"])).total_seconds(), 1
        )
        app_repo.save_job_meta(job_id, meta)


def _update_engine_meta(job_id: str, engine: str, **kwargs) -> None:
    with _meta_update_lock:
        app_repo = app_store.get_app_repository()
        meta = app_repo.load_job_meta(job_id) or {}
        engines = dict(meta.get("engines") or {})
        state = dict(engines.get(engine) or {"job_id": job_id, "stats": {}})
        state.update(kwargs)
        engines[engine] = state
        meta["engines"] = engines
        if meta.get("created_at"):
            meta["elapsed_seconds"] = round(
                (datetime.now(timezone.utc) - datetime.fromisoformat(meta["created_at"])).total_seconds(), 1
            )
        app_repo.save_job_meta(job_id, meta)


def _load_job_meta(job_id: str) -> dict | None:
    return app_store.get_app_repository().load_job_meta(job_id)


def _is_cancelled(job_id: str) -> bool:
    if _cancel_flags.get(job_id):
        return True
    meta = _load_job_meta(job_id)
    return bool(meta and meta.get("status") == "cancelled")


def _index_write_is_allowed(job_id: str, doc_id: str) -> bool:
    """Return whether a worker may still publish index data for a document.

    Document/account deletion first marks active jobs as cancelled and then
    removes the application row. Checking both signals closes both orderings of
    that race: a worker that has already loaded the document sees the
    cancellation marker, while a worker with stale job metadata still sees the
    missing document row.
    """

    if _is_cancelled(job_id):
        return False
    return app_store.get_app_repository().get_document(doc_id) is not None


def _assert_index_write_allowed(job_id: str, doc_id: str) -> None:
    if not _index_write_is_allowed(job_id, doc_id):
        raise _IndexCancelled("Document was deleted or indexing was cancelled")


def _save_job_artifact(job_id: str, name: str, data) -> None:
    with _artifact_update_lock:
        if _is_cancelled(job_id):
            raise _IndexCancelled("Indexing cancelled")
        job_dir = fs.job_dir(job_id)
        fs.write_json(job_dir / name, data)
        blob_repo = blob_store.get_blob_repository()
        blob_ref = blob_repo.save_json(f"jobs/{job_id}/{name}", data)
        if _is_cancelled(job_id):
            blob_repo.delete(blob_ref)
            (job_dir / name).unlink(missing_ok=True)
            raise _IndexCancelled("Indexing cancelled")
        with _meta_update_lock:
            meta = _load_job_meta(job_id) or {}
            artifacts = dict(meta.get("artifacts") or {})
            artifacts[name] = blob_ref
            meta["artifacts"] = artifacts
            app_store.get_app_repository().save_job_meta(job_id, meta)


def _job_input_path(job_id: str, doc: dict, fallback_path: Path) -> Path:
    blob_ref = doc.get("blob_ref") or ({"key": doc.get("blob_key")} if doc.get("blob_key") else None)
    if blob_ref:
        suffix = Path(doc.get("filename", "input")).suffix
        target = fs.job_dir(job_id) / "input" / f"{doc['doc_id']}{suffix}"
        downloaded = blob_store.get_blob_repository().download_to_path(blob_ref, target)
        size_bytes = downloaded.stat().st_size
        head = downloaded.read_bytes()[:4096]
        ok, _, message = document_service.validate_upload(doc.get("filename", ""), size_bytes)
        if ok:
            ok, _, message = document_service.validate_upload_content(
                doc.get("filename", ""),
                doc.get("content_type"),
                head,
                size_bytes,
            )
        declared_ext = Path(str(doc.get("filename") or "")).suffix.lower().lstrip(".")
        detected_image = document_service.detect_supported_image_format(head)
        if not ok and declared_ext in {"png", "jpg", "jpeg"} and detected_image:
            actual_ext, actual_mime = detected_image
            actual_filename = f"{Path(str(doc.get('filename') or 'image')).stem}.{actual_ext}"
            ok, _, message = document_service.validate_upload_content(
                actual_filename,
                actual_mime,
                head,
                size_bytes,
            )
            if ok and downloaded.suffix.lower() != f".{actual_ext}":
                corrected_path = downloaded.with_suffix(f".{actual_ext}")
                downloaded.replace(corrected_path)
                downloaded = corrected_path
        if not ok:
            downloaded.unlink(missing_ok=True)
            raise ValueError(f"Downloaded upload validation failed: {message}")
        return downloaded
    return fallback_path


def _lightrag_tenant_for_document(doc: dict, *, persist: bool = False) -> str:
    return document_service.lightrag_tenant_for_document(doc, persist=persist)


def _lightrag_pages(filename: str, pages: list) -> list[dict]:
    result: list[dict] = []
    for position, page in enumerate(pages, start=1):
        if isinstance(page, dict):
            text = str(page.get("text") or page.get("content") or "")
            human_page = int(page.get("page") or position)
        else:
            text = str(getattr(page, "text", "") or "")
            parser_page = int(getattr(page, "page_idx", position - 1))
            human_page = parser_page + 1
        if not text.strip():
            continue
        result.append({
            "page": human_page,
            "text": text,
            "source_path": f"{filename}#page={human_page}",
        })
    return result


def _reusable_parsed_pages(doc_id: str, current_job_id: str) -> list[dict] | None:
    """Load the newest shared parse artifact for a LightRAG-only retry/backfill."""

    app_repo = app_store.get_app_repository()
    blob_repo = blob_store.get_blob_repository()
    candidates = sorted(
        (
            meta
            for meta in app_repo.list_all_jobs()
            if meta.get("job_id") != current_job_id
            and str(meta.get("doc_id") or "") == str(doc_id)
        ),
        key=lambda meta: str(meta.get("created_at") or ""),
        reverse=True,
    )
    for meta in candidates:
        artifact = (meta.get("artifacts") or {}).get("parsed_pages.json")
        value = None
        if isinstance(artifact, dict):
            value = blob_repo.read_json(
                artifact.get("key") or artifact.get("pathname") or ""
            )
        if value is None and meta.get("job_id"):
            value = fs.read_json(fs.job_dir(str(meta["job_id"])) / "parsed_pages.json")
        if isinstance(value, list) and value:
            normalized = _lightrag_pages("document", value)
            if normalized:
                return normalized
    return None


def _run_legacy_engine(
    job_id: str,
    doc: dict,
    content_list: list,
    pages: list,
    block_types: dict,
    start_time: float,
) -> dict:
    """Build the existing graph from a shared parse result."""
    from pipeline.entity_extractor import create_model, extract_entities
    from pipeline.kg_builder import build_kg, extractions_to_records

    doc_id = str(doc["doc_id"])
    total_pages = len(pages)
    _update_engine_meta(job_id, "legacy", status="indexing", error=None)
    document_service.update_engine_index_status(
        doc_id, "legacy", "indexing", job_id=job_id, pages=total_pages,
    )

    model = create_model()
    annotated_docs = []
    total_entities = 0
    for i, page in enumerate(pages):
        if _is_cancelled(job_id):
            raise _IndexCancelled("Indexing cancelled")
        _update_meta(
            job_id,
            stage=f"Dual indexing: classic extraction {i + 1}/{total_pages}",
            progress={
                "parsed_pages": total_pages,
                "total_pages": total_pages,
                "extracted_entities": total_entities,
            },
        )
        ann_doc = extract_entities(page.text, model)
        annotated_docs.append(ann_doc)
        total_entities += len(ann_doc.extractions) if ann_doc.extractions else 0

    records = extractions_to_records(pages, annotated_docs, doc_id)
    _save_job_artifact(job_id, "extractions.json", records)
    nodes, edges = build_kg(pages, annotated_docs, doc_id)
    chunks = _chunks_from_pages(doc_id, pages)
    _attach_embeddings(nodes, chunks)
    graph_repo = graph_store.get_graph_repository()
    _assert_index_write_allowed(job_id, doc_id)
    graph_repo.upsert_document_graph(
        document={**doc, "doc_id": doc_id, "status": "indexed"},
        nodes=nodes,
        edges=edges,
        chunks=chunks,
    )
    try:
        # Deletion may have raced with the database upsert. Roll the write back
        # before the child can advertise a successful classic index.
        _assert_index_write_allowed(job_id, doc_id)
    except _IndexCancelled:
        graph_repo.remove_document(doc_id)
        raise

    alignment_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for record in records:
        alignment = record.get("alignment") or "null"
        alignment_counts[alignment] = alignment_counts.get(alignment, 0) + 1
        entity_type = record.get("type", "UNKNOWN")
        type_counts[entity_type] = type_counts.get(entity_type, 0) + 1

    stats = {
        "blocks": len(content_list),
        "block_types": block_types,
        "pages": total_pages,
        "raw_extractions": len(records),
        "nodes": len(nodes),
        "edges": len(edges),
        "type_counts": type_counts,
        "alignment_counts": alignment_counts,
        "elapsed_seconds": round(time.time() - start_time, 1),
    }
    _save_job_artifact(job_id, "kg_nodes.json", nodes)
    _save_job_artifact(job_id, "kg_edges.json", edges)
    _save_job_artifact(job_id, "stats.json", stats)
    try:
        _assert_index_write_allowed(job_id, doc_id)
    except _IndexCancelled:
        graph_repo.remove_document(doc_id)
        raise
    _update_engine_meta(job_id, "legacy", status="done", error=None, stats=stats)
    document_service.update_engine_index_status(
        doc_id, "legacy", "done", job_id=job_id, stats=stats, pages=total_pages,
    )
    try:
        # A cancel can land between the final guard and the metadata update.
        # Keep the graph and engine state symmetric in that narrow window.
        _assert_index_write_allowed(job_id, doc_id)
    except _IndexCancelled:
        graph_repo.remove_document(doc_id)
        _update_engine_meta(job_id, "legacy", status="cancelled", error=None)
        document_service.update_engine_index_status(
            doc_id, "legacy", "cancelled", job_id=job_id,
        )
        raise
    return stats


def _run_lightrag_engine(job_id: str, doc: dict, pages: list) -> dict:
    """Insert stable page documents through the private LightRAG facade."""
    from services import lightrag_service

    doc_id = str(doc["doc_id"])
    if not lightrag_service.enabled():
        _update_engine_meta(job_id, "lightrag", status="disabled", error=None)
        document_service.update_engine_index_status(
            doc_id, "lightrag", "disabled", job_id=job_id,
        )
        return {"status": "disabled"}
    if _is_cancelled(job_id):
        raise _IndexCancelled("Indexing cancelled")

    _update_engine_meta(job_id, "lightrag", status="indexing", error=None)
    document_service.update_engine_index_status(
        doc_id, "lightrag", "indexing", job_id=job_id, pages=len(pages),
    )
    tenant_id = _lightrag_tenant_for_document(doc, persist=True)
    result = async_bridge.run(lightrag_service.index_pages(
        tenant_id=tenant_id,
        doc_id=doc_id,
        filename=str(doc.get("filename") or doc_id),
        pages=_lightrag_pages(str(doc.get("filename") or doc_id), pages),
    ))
    if _is_cancelled(job_id):
        # The remote insertion is idempotent. Deleting the completed child
        # gives cancellation the same externally visible semantics.
        async_bridge.run(lightrag_service.delete_document(
            tenant_id=tenant_id,
            doc_id=doc_id,
            page_count=len(pages),
            page_ids=result.get("page_ids") or None,
        ))
        raise _IndexCancelled("Indexing cancelled")

    stats = dict(result.get("stats") or {})
    stats["indexed_pages"] = int(result.get("indexed_pages") or len(result.get("pages") or []))
    if result.get("page_ids"):
        stats["page_ids"] = list(result["page_ids"])
    stats.setdefault("pages", len(_lightrag_pages(str(doc.get("filename") or doc_id), pages)))
    try:
        graph = async_bridge.run(lightrag_service.export_graph(
            tenant_id=tenant_id,
            doc_id=doc_id,
            allowed_doc_ids={doc_id},
            max_nodes=int(os.getenv("LIGHTRAG_EXPORT_MAX_NODES", "10000")),
            max_edges=int(os.getenv("LIGHTRAG_EXPORT_MAX_EDGES", "100000")),
        ))
        stats["nodes"] = int(graph.get("total_nodes") or len(graph.get("nodes") or []))
        stats["edges"] = int(graph.get("total_edges") or len(graph.get("edges") or []))
    except Exception:
        # Successful insertion remains successful even if the optional stats
        # snapshot is temporarily unavailable.
        stats.setdefault("nodes", 0)
        stats.setdefault("edges", 0)
    _update_engine_meta(job_id, "lightrag", status="done", error=None, stats=stats)
    document_service.update_engine_index_status(
        doc_id, "lightrag", "done", job_id=job_id, stats=stats, pages=len(pages),
    )
    return stats


def _run_lightrag_retry_from_artifact(
    job_id: str,
    doc: dict,
    pages: list[dict],
    start_time: float,
) -> None:
    """Finish a LightRAG-only retry without parsing the upload again."""

    doc_id = str(doc["doc_id"])
    total_pages = len(pages)
    _save_job_artifact(job_id, "parsed_pages.json", pages)
    _update_meta(
        job_id,
        status="indexing",
        stage="Reusing shared parse artifact for LightRAG retry...",
        progress={
            "parsed_pages": total_pages,
            "total_pages": total_pages,
            "extracted_entities": 0,
        },
    )
    try:
        _run_lightrag_engine(job_id, doc, pages)
    except _IndexCancelled:
        _update_engine_meta(job_id, "lightrag", status="cancelled", error=None)
        document_service.update_engine_index_status(
            doc_id, "lightrag", "cancelled", job_id=job_id,
        )
        _update_meta(job_id, status="cancelled", stage="Cancelled", error=None)
        return
    except Exception as exc:
        message = str(exc)
        _update_engine_meta(job_id, "lightrag", status="failed", error=message)
        document_service.update_engine_index_status(
            doc_id,
            "lightrag",
            "failed",
            job_id=job_id,
            error=message,
            pages=total_pages,
        )
        _update_meta(
            job_id,
            status="failed",
            stage="LightRAG retry failed",
            error=message,
            elapsed_seconds=round(time.time() - start_time, 1),
        )
        report_event(
            "index_engine_failed",
            "LightRAG retry failed while the classic index was preserved",
            severity="error",
            source="index_worker",
            context={
                "job_id": job_id,
                "doc_id": doc_id,
                "engine": "lightrag",
                "error": message,
            },
        )
        return

    if _is_cancelled(job_id):
        _update_meta(job_id, status="cancelled", stage="Cancelled", error=None)
        return
    _update_meta(
        job_id,
        status="done",
        stage="LightRAG index complete",
        progress={
            "parsed_pages": total_pages,
            "total_pages": total_pages,
            "extracted_entities": 0,
        },
        elapsed_seconds=round(time.time() - start_time, 1),
        error=None,
    )


def _run_pipeline(job_id: str) -> None:
    meta = _load_job_meta(job_id)
    if not meta:
        return

    doc_id = meta["doc_id"]
    doc = app_store.get_app_repository().get_document(doc_id)
    if not doc:
        _update_meta(job_id, status="failed", stage=f"Error: Document '{doc_id}' not found", error=f"Document '{doc_id}' not found")
        return

    job_dir = fs.job_dir(job_id)
    start_time = time.time()

    target_engines = set(meta.get("target_engines") or {"legacy", "lightrag"})
    if target_engines == {"lightrag"}:
        reusable_pages = _reusable_parsed_pages(doc_id, job_id)
        if reusable_pages:
            _run_lightrag_retry_from_artifact(
                job_id,
                {**doc, "doc_id": doc_id},
                reusable_pages,
                start_time,
            )
            return

    try:
        pdf_path = _job_input_path(job_id, doc, Path(meta["pdf_path"]))

        # ── Stage 1: parsing ──────────────────────────────────────────────
        if _is_cancelled(job_id):
            _update_meta(job_id, status="cancelled", stage="Cancelled")
            return

        use_mineru = _should_use_mineru_for_document(doc)
        parser_label = "MinerU document parsing" if use_mineru else "Local document parsing"
        _update_meta(job_id, status="parsing", stage=f"{parser_label}...")
        mineru_out_dir = job_dir / "mineru_output"
        mineru_out_dir.mkdir(parents=True, exist_ok=True)

        def _parse_progress(state: str, progress: dict) -> None:
            parsed_pages = int(progress.get("extracted_pages") or 0)
            total_pages = int(progress.get("total_pages") or 0)
            _update_meta(
                job_id,
                stage=f"{parser_label} ({state})...",
                progress={
                    "parsed_pages": parsed_pages,
                    "total_pages": total_pages,
                    "extracted_entities": 0,
                },
            )

        if use_mineru:
            client = MinerUCloudClient()
            content_list_path = client.parse_local_file(
                pdf_path,
                mineru_out_dir,
                data_id=job_id,
                language=doc.get("language", "ch"),
                enable_formula=bool(doc.get("enable_formula", True)),
                enable_table=bool(doc.get("enable_table", True)),
                progress_callback=_parse_progress,
            )
        else:
            content_list_path = local_parser.parse_local_file(
                pdf_path,
                mineru_out_dir,
                data_id=job_id,
                language=doc.get("language", "ch"),
                enable_formula=bool(doc.get("enable_formula", True)),
                enable_table=bool(doc.get("enable_table", True)),
                progress_callback=_parse_progress,
            )

        # ── Stage 2: shared parsed pages ──────────────────────────────────
        if _is_cancelled(job_id):
            _update_meta(job_id, status="cancelled", stage="Cancelled")
            return

        from pipeline.text_assembler import load_content_list, assemble_pages, count_blocks_by_type

        content_list = load_content_list(content_list_path)
        pages = assemble_pages(content_list)
        total_pages = len(pages)
        block_types = count_blocks_by_type(content_list)
        _save_job_artifact(
            job_id,
            "parsed_pages.json",
            _lightrag_pages(str(doc.get("filename") or doc_id), pages),
        )

        _update_meta(
            job_id,
            status="indexing",
            stage="Shared parse complete; starting both engines...",
            progress={"parsed_pages": total_pages, "total_pages": total_pages, "extracted_entities": 0},
        )
        target_engines = set(meta.get("target_engines") or {"legacy", "lightrag"})
        if "legacy" in target_engines:
            document_service.update_engine_index_status(
                doc_id, "legacy", "indexing", job_id=job_id, pages=total_pages,
            )

        # A user still owns one parent job and therefore one concurrency slot;
        # the two children fan out only after parsing has completed once.
        child_errors: dict[str, str] = {}
        child_results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"dual-index-{job_id}") as executor:
            futures = {}
            if "legacy" in target_engines:
                futures[executor.submit(
                    _run_legacy_engine,
                    job_id,
                    {**doc, "doc_id": doc_id},
                    content_list,
                    pages,
                    block_types,
                    start_time,
                )] = "legacy"
            if "lightrag" in target_engines:
                futures[executor.submit(
                    _run_lightrag_engine,
                    job_id,
                    {**doc, "doc_id": doc_id},
                    pages,
                )] = "lightrag"
            for future in as_completed(futures):
                engine = futures[future]
                try:
                    child_results[engine] = future.result()
                except _IndexCancelled:
                    child_errors[engine] = "Indexing cancelled"
                    _update_engine_meta(job_id, engine, status="cancelled", error=None)
                    document_service.update_engine_index_status(
                        doc_id, engine, "cancelled", job_id=job_id,
                    )
                except Exception as child_exc:
                    child_errors[engine] = str(child_exc)
                    _update_engine_meta(job_id, engine, status="failed", error=str(child_exc))
                    document_service.update_engine_index_status(
                        doc_id, engine, "failed", job_id=job_id, error=str(child_exc), pages=total_pages,
                    )
                    report_event(
                        "index_engine_failed",
                        f"{engine} indexing failed while the sibling engine was preserved",
                        severity="error",
                        source="index_worker",
                        context={"job_id": job_id, "doc_id": doc_id, "engine": engine, "error": str(child_exc)},
                    )

        if _is_cancelled(job_id):
            _update_meta(job_id, status="cancelled", stage="Cancelled", error=None)
            return

        final_meta = _load_job_meta(job_id) or {}
        engine_states = final_meta.get("engines") or {}
        statuses = {
            name: state.get("status")
            for name, state in engine_states.items()
            if name in target_engines
        }
        completed = {name for name, status in statuses.items() if status == "done"}
        disabled = {name for name, status in statuses.items() if status == "disabled"}
        failed = {name for name, status in statuses.items() if status == "failed"}

        legacy_stats = child_results.get("legacy") or {}
        extracted_entities = int(legacy_stats.get("raw_extractions") or 0)
        if failed and completed:
            parent_status = "partial"
            stage = "Complete with an engine failure; retry the failed engine only"
        elif failed:
            parent_status = "failed"
            stage = "Requested index engine(s) failed"
        elif not completed and disabled:
            parent_status = "failed"
            stage = "Requested index engine is disabled"
        else:
            parent_status = "done"
            if target_engines == {"legacy", "lightrag"}:
                stage = "Both index engines complete" if statuses.get("lightrag") == "done" else "Classic index complete; LightRAG disabled"
            else:
                stage = f"{next(iter(target_engines))} index complete"
        _update_meta(
            job_id,
            status=parent_status,
            stage=stage,
            error="; ".join(f"{name}: {message}" for name, message in child_errors.items()) or None,
            progress={
                "parsed_pages": total_pages,
                "total_pages": total_pages,
                "extracted_entities": extracted_entities,
            },
        )

    except Exception as exc:
        _update_meta(job_id, status="failed", stage=f"Error: {exc}", error=str(exc))
        target_engines = set((meta or {}).get("target_engines") or {"legacy", "lightrag"})
        for engine in target_engines:
            state = ((_load_job_meta(job_id) or {}).get("engines") or {}).get(engine, {})
            if state.get("status") not in {"done", "disabled"}:
                _update_engine_meta(job_id, engine, status="failed", error=str(exc))
                document_service.update_engine_index_status(
                    doc_id, engine, "failed", job_id=job_id, error=str(exc),
                )
        update_doc_status(doc_id, "failed")
    finally:
        _active_threads.pop(job_id, None)
        _cancel_flags.pop(job_id, None)


def get_job_status(job_id: str) -> dict | None:
    return _load_job_meta(job_id)


def get_job_result(job_id: str) -> dict | None:
    meta = _load_job_meta(job_id)
    if not meta:
        return None
    if meta["status"] not in {"done", "partial"}:
        return meta

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

    return {
        "job_id": meta["job_id"],
        "doc_id": meta["doc_id"],
        "status": meta["status"],
        "engines": meta.get("engines", {}),
        "stats": stats,
        "extractions": extractions,
        "nodes": nodes,
        "edges": edges,
    }


def _cleanup_completed_child(meta: dict, engine: str) -> None:
    """Remove output already published by one child of an active parent job."""

    doc_id = str(meta.get("doc_id") or "")
    if not doc_id:
        return
    doc = app_store.get_app_repository().get_document(doc_id)
    if engine == "legacy":
        graph_store.get_graph_repository().remove_document(doc_id)
        return
    if engine != "lightrag" or not doc:
        return

    from services import lightrag_deletion_service

    doc_for_delete = {**doc, "indexes": document_service.normalized_indexes(doc)}
    doc_for_delete["lightrag_tenant_id"] = document_service.lightrag_tenant_for_document(doc)
    lightrag_deletion_service.delete_or_schedule(doc_for_delete)


def cancel_job(job_id: str, *, cleanup_completed: bool = True) -> tuple[bool, str]:
    meta = _load_job_meta(job_id)
    if not meta:
        return False, "not_found"
    prev_status = str(meta.get("status") or "")
    # Completed/failed parents are immutable. In particular, a late DELETE
    # request must not erase a healthy index that is no longer running.
    if prev_status in _TERMINAL_JOB_STATUSES:
        return True, prev_status

    _cancel_flags[job_id] = True
    _update_meta(job_id, status="cancelled", stage="Cancelled by user")
    doc_id = str(meta.get("doc_id") or "")
    for engine in set(meta.get("target_engines") or {"legacy", "lightrag"}):
        state = (meta.get("engines") or {}).get(engine) or {}
        state_status = str(state.get("status") or "")
        if state_status == "done" and cleanup_completed:
            _cleanup_completed_child(meta, engine)
        # During document/account deletion the outer deletion operation owns
        # completed-child cleanup. Preserve its done marker so the durable
        # LightRAG deletion service still has page identifiers to consume.
        if state_status == "done" and not cleanup_completed:
            continue
        if state_status not in {"disabled", "failed"}:
            _update_engine_meta(job_id, engine, status="cancelled", error=None)
            if doc_id:
                document_service.update_engine_index_status(
                    doc_id, engine, "cancelled", job_id=job_id,
                )
    return True, prev_status


def cancel_document_jobs(doc_id: str, *, detach: bool = False) -> list[str]:
    """Cancel every active index job for a document.

    When a document row is about to disappear, ``detach`` moves the cancelled
    job metadata to a tombstone doc id. Postgres tenant deletion consequently
    leaves that marker in place, so a queued or already-running Worker can
    still observe cancellation and cannot resurrect either index.
    """

    repo = app_store.get_app_repository()
    cancelled: list[str] = []
    for meta in list(repo.list_all_jobs()):
        if meta.get("job_type") == "lightrag_delete":
            continue
        if str(meta.get("doc_id") or "") != str(doc_id):
            continue
        if str(meta.get("status") or "") not in _ACTIVE_JOB_STATUSES:
            continue
        job_id = str(meta.get("job_id") or "")
        if not job_id:
            continue
        ok, _ = cancel_job(job_id, cleanup_completed=not detach)
        if not ok:
            continue
        cancelled.append(job_id)
        if detach:
            _update_meta(
                job_id,
                doc_id=f"__cancelled_index__:{doc_id}:{job_id}",
                source_doc_id=str(doc_id),
                deletion_cancelled=True,
                status="cancelled",
                stage="Cancelled because the source document was deleted",
            )
    return cancelled


def purge_cancelled_job_artifacts(job_ids: list[str] | set[str]) -> None:
    """Erase source-derived artifacts while retaining cancellation tombstones."""

    repo = app_store.get_app_repository()
    blob_repo = blob_store.get_blob_repository()
    with _artifact_update_lock:
        for job_id in job_ids:
            meta = repo.load_job_meta(str(job_id))
            if not meta or meta.get("status") != "cancelled":
                continue
            for artifact in (meta.get("artifacts") or {}).values():
                if isinstance(artifact, dict):
                    blob_repo.delete(artifact)
            fs.delete_job(str(job_id))
            # Retain only the non-content fence required by a delayed Worker.
            # In particular, do not preserve filenames, paths, actor/tenant
            # identifiers, parse progress, model errors, or page-derived stats
            # after an account-data deletion.
            tombstone = {
                "job_id": str(job_id),
                "job_type": "cancelled_index",
                "doc_id": str(meta.get("doc_id") or f"__cancelled_index__:{job_id}"),
                "source_doc_id": str(meta.get("source_doc_id") or ""),
                "status": "cancelled",
                "stage": "Cancelled because the source document was deleted",
                "created_at": meta.get("created_at"),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "deletion_cancelled": True,
                "artifacts": {},
                "artifacts_erased_at": datetime.now(timezone.utc).isoformat(),
            }
            repo.save_job_meta(str(job_id), tombstone)


def count_active_jobs() -> int:
    return sum(1 for t in _active_threads.values() if t.is_alive())


def run_queued_job(job_id: str) -> dict | None:
    meta = _load_job_meta(job_id)
    if not meta or meta.get("status") in _TERMINAL_JOB_STATUSES:
        return meta
    _cancel_flags[job_id] = False
    _run_pipeline(job_id)
    return get_job_status(job_id)


def process_next_index_job(timeout_seconds: int = 5) -> dict | None:
    queue_repo = queue_store.get_queue_repository()
    recovery = getattr(queue_repo, "recover_expired_jobs", lambda: {"recovered": [], "exhausted": []})()
    app_repo = app_store.get_app_repository()

    for job_id in recovery.get("recovered", []):
        meta = app_repo.load_job_meta(job_id)
        if not meta or meta.get("status") in {"done", "partial", "cancelled"}:
            continue
        _update_meta(job_id, status="queued", stage="Worker interrupted; queued for automatic recovery")
        report_event(
            "index_job_recovered",
            "An interrupted indexing job was returned to the durable queue",
            severity="warning",
            source="index_worker",
            context={"job_id": job_id},
        )

    for job_id in recovery.get("exhausted", []):
        meta = app_repo.load_job_meta(job_id)
        if not meta or meta.get("status") in {"done", "partial", "cancelled"}:
            continue
        error = "Index worker was interrupted repeatedly and exhausted automatic recovery attempts"
        _update_meta(job_id, status="failed", stage=f"Error: {error}", error=error)
        if meta.get("job_type") == "lightrag_delete":
            report_event(
                "lightrag_delete_recovery_exhausted",
                "A LightRAG deletion tombstone needs manual retry",
                source="index_worker",
                context={"job_id": job_id, "source_doc_id": meta.get("source_doc_id")},
            )
        elif meta.get("doc_id"):
            for engine in set(meta.get("target_engines") or {"legacy", "lightrag"}):
                _update_engine_meta(job_id, engine, status="failed", error=error)
                document_service.update_engine_index_status(
                    meta["doc_id"], engine, "failed", job_id=job_id, error=error,
                )
            update_doc_status(meta["doc_id"], "failed")
        if meta.get("job_type") != "lightrag_delete":
            report_event(
                "index_job_recovery_exhausted",
                error,
                source="index_worker",
                context={"job_id": job_id},
            )

    payload = queue_repo.pop_index_job(timeout_seconds)
    if not payload:
        return None
    try:
        if payload.get("job_type") == "lightrag_delete":
            from services import lightrag_deletion_service

            result = lightrag_deletion_service.run_delete_job(payload["job_id"])
        else:
            job_id = str(payload["job_id"])
            meta = app_repo.load_job_meta(job_id)
            if not meta or meta.get("status") in _TERMINAL_JOB_STATUSES:
                result = meta
            else:
                owner_id = str(meta.get("owner_id") or "default")
                if not _owner_lock(queue_repo, owner_id, job_id):
                    # Another worker is processing a parent job for this
                    # tenant. Put this claim back without consuming a recovery
                    # attempt; only one parent (with two engine children) runs.
                    retry_payload = {
                        key: value
                        for key, value in payload.items()
                        if key != "_queue_receipt"
                    }
                    queue_repo.enqueue_index_job(retry_payload)
                    _update_meta(
                        job_id,
                        status="queued",
                        stage="Waiting for this tenant's current indexing task",
                    )
                    result = app_repo.load_job_meta(job_id)
                else:
                    stop, heartbeat = _owner_lock_heartbeat(
                        queue_repo,
                        owner_id,
                        job_id,
                        payload,
                    )
                    try:
                        result = run_queued_job(job_id)
                    finally:
                        stop.set()
                        if heartbeat:
                            heartbeat.join(timeout=1)
                        _release_owner_lock(queue_repo, owner_id, job_id)
    except Exception:
        # Leave the processing receipt leased. Upstash recovery will requeue
        # either kind of job with a bounded attempt count instead of losing it.
        raise
    else:
        getattr(queue_repo, "ack_index_job", lambda _payload: None)(payload)
        return result
