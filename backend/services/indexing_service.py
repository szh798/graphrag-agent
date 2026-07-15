"""Indexing Service — Pipeline orchestration (parsing → extracting → indexing)."""
from __future__ import annotations

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
from services import document_service
from services.document_service import update_doc_status
from services.mineru_cloud_client import MinerUCloudClient
from services import local_parser
from storage import graph_repository as graph_store
from storage import queue_repository as queue_store
from pipeline.embeddings import embed_texts

load_dotenv(Path(__file__).parent.parent / ".env", override=False)

# In-memory registry of active jobs {job_id: threading.Thread}
_active_threads: dict[str, threading.Thread] = {}
_cancel_flags: dict[str, bool] = {}


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


def start_indexing(doc_id: str) -> dict:
    app_repo = app_store.get_app_repository()
    doc = app_repo.get_document(doc_id)
    if not doc:
        return None  # type: ignore

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    queue_repo = queue_store.get_queue_repository()
    queued = bool(getattr(queue_repo, "is_durable", lambda: False)())

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
    }
    app_repo.save_job_meta(job_id, meta)

    if queued:
        queue_repo.enqueue_index_job({"job_id": job_id, "doc_id": doc_id})
        update_status = getattr(app_repo, "update_document_status", None)
        if callable(update_status):
            update_status(doc_id, "queued")
        return meta

    _cancel_flags[job_id] = False
    thread = threading.Thread(target=_run_pipeline, args=(job_id,), daemon=True)
    _active_threads[job_id] = thread
    thread.start()

    return meta


def _update_meta(job_id: str, **kwargs) -> None:
    app_repo = app_store.get_app_repository()
    meta = app_repo.load_job_meta(job_id) or {}
    meta.update(kwargs)
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


def _save_job_artifact(job_id: str, name: str, data) -> None:
    job_dir = fs.job_dir(job_id)
    fs.write_json(job_dir / name, data)
    blob_ref = blob_store.get_blob_repository().save_json(f"jobs/{job_id}/{name}", data)
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
        if not ok:
            downloaded.unlink(missing_ok=True)
            raise ValueError(f"Downloaded upload validation failed: {message}")
        return downloaded
    return fallback_path


def _run_pipeline(job_id: str) -> None:
    meta = _load_job_meta(job_id)
    if not meta:
        return

    doc_id = meta["doc_id"]
    doc = app_store.get_app_repository().get_document(doc_id)
    if not doc:
        _update_meta(job_id, status="failed", stage=f"Error: Document '{doc_id}' not found", error=f"Document '{doc_id}' not found")
        return

    pdf_path = _job_input_path(job_id, doc, Path(meta["pdf_path"]))
    job_dir = fs.job_dir(job_id)
    start_time = time.time()

    try:
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

        # ── Stage 2: extracting ───────────────────────────────────────────
        if _is_cancelled(job_id):
            _update_meta(job_id, status="cancelled", stage="Cancelled")
            return

        from pipeline.text_assembler import load_content_list, assemble_pages, count_blocks_by_type
        from pipeline.entity_extractor import create_model, extract_entities
        from pipeline.kg_builder import build_kg, extractions_to_records

        content_list = load_content_list(content_list_path)
        pages = assemble_pages(content_list)
        total_pages = len(pages)
        block_types = count_blocks_by_type(content_list)

        _update_meta(
            job_id,
            status="extracting",
            stage=f"Extracting entities (LangExtract + LLM)...",
            progress={"parsed_pages": total_pages, "total_pages": total_pages, "extracted_entities": 0},
        )
        update_doc_status(doc_id, "indexing", pages=total_pages)

        model = create_model()
        annotated_docs = []
        total_entities = 0

        for i, page in enumerate(pages):
            if _is_cancelled(job_id):
                _update_meta(job_id, status="cancelled", stage="Cancelled")
                return

            _update_meta(
                job_id,
                stage=f"Extracting entities page {i+1}/{total_pages} (LangExtract + LLM)...",
                progress={"parsed_pages": total_pages, "total_pages": total_pages,
                          "extracted_entities": total_entities},
            )
            ann_doc = extract_entities(page.text, model)
            annotated_docs.append(ann_doc)
            total_entities += len(ann_doc.extractions) if ann_doc.extractions else 0

        # Save raw extractions
        records = extractions_to_records(pages, annotated_docs, doc_id)
        _save_job_artifact(job_id, "extractions.json", records)

        # ── Stage 3: indexing ─────────────────────────────────────────────
        _update_meta(job_id, status="indexing", stage="Building knowledge graph...")

        nodes, edges = build_kg(pages, annotated_docs, doc_id)
        fs.write_json(job_dir / "kg_nodes.json", nodes)
        fs.write_json(job_dir / "kg_edges.json", edges)

        chunks = _chunks_from_pages(doc_id, pages)
        _attach_embeddings(nodes, chunks)
        graph_store.get_graph_repository().upsert_document_graph(
            document={**doc, "doc_id": doc_id, "status": "indexed"},
            nodes=nodes,
            edges=edges,
            chunks=chunks,
        )

        # Count alignment types
        alignment_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for r in records:
            al = r.get("alignment") or "null"
            alignment_counts[al] = alignment_counts.get(al, 0) + 1
            t = r.get("type", "UNKNOWN")
            type_counts[t] = type_counts.get(t, 0) + 1

        elapsed = round(time.time() - start_time, 1)
        stats = {
            "blocks": len(content_list),
            "block_types": block_types,
            "pages": total_pages,
            "raw_extractions": len(records),
            "nodes": len(nodes),
            "edges": len(edges),
            "type_counts": type_counts,
            "alignment_counts": alignment_counts,
            "elapsed_seconds": elapsed,
        }
        _save_job_artifact(job_id, "kg_nodes.json", nodes)
        _save_job_artifact(job_id, "kg_edges.json", edges)
        _save_job_artifact(job_id, "stats.json", stats)

        _update_meta(
            job_id,
            status="done",
            stage="Complete",
            progress={"parsed_pages": total_pages, "total_pages": total_pages,
                      "extracted_entities": len(records)},
        )
        update_doc_status(doc_id, "indexed", pages=total_pages)

    except Exception as exc:
        _update_meta(job_id, status="failed", stage=f"Error: {exc}", error=str(exc))
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
    if meta["status"] != "done":
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
        "status": "done",
        "stats": stats,
        "extractions": extractions,
        "nodes": nodes,
        "edges": edges,
    }


def cancel_job(job_id: str) -> tuple[bool, str]:
    meta = _load_job_meta(job_id)
    if not meta:
        return False, "not_found"
    prev_status = meta["status"]
    _cancel_flags[job_id] = True
    _update_meta(job_id, status="cancelled", stage="Cancelled by user")
    return True, prev_status


def count_active_jobs() -> int:
    return sum(1 for t in _active_threads.values() if t.is_alive())


def run_queued_job(job_id: str) -> dict | None:
    meta = _load_job_meta(job_id)
    if not meta or meta.get("status") == "cancelled":
        return meta
    _cancel_flags[job_id] = False
    _run_pipeline(job_id)
    return get_job_status(job_id)


def process_next_index_job(timeout_seconds: int = 5) -> dict | None:
    payload = queue_store.get_queue_repository().pop_index_job(timeout_seconds)
    if not payload:
        return None
    return run_queued_job(payload["job_id"])
