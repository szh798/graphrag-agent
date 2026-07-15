"""QA Service — Agentic-RAG wrapper."""
from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from storage import app_repository as app_store
from storage import file_store as fs
from storage import graph_repository as graph_store
from storage.account_repository import get_account_repository
from pipeline.embeddings import embed_text
from pipeline.llm_config import LLM_MODEL, LLM_PROVIDER
from observability import get_request_id


logger = logging.getLogger(__name__)
PUBLIC_QA_ERROR = "QA service is temporarily unavailable."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_title(question: str) -> str:
    title = " ".join(question.strip().split())
    if not title:
        return "新对话"
    return title[:36] + ("..." if len(title) > 36 else "")


def _session_summary(session: dict) -> dict:
    messages = session.get("messages", [])
    return {
        "id": session["id"],
        "title": session.get("title") or "新对话",
        "created_at": session.get("created_at"),
        "updated_at": session.get("updated_at"),
        "message_count": len(messages),
        "last_question": session.get("last_question", ""),
        "last_answer": session.get("last_answer", ""),
    }


def _session_history(session: dict) -> list[dict]:
    history = []
    for msg in session.get("messages", []):
        role = msg.get("role")
        content = msg.get("content", "")
        if role in {"human", "ai"} and content:
            history.append({"role": role, "content": content})
    return history


def _new_message(role: str, content: str, **extra) -> dict:
    msg = {
        "id": f"m_{uuid.uuid4().hex[:10]}",
        "role": role,
        "content": content,
        "timestamp": _now(),
    }
    msg.update(extra)
    return msg


def _should_use_question_embedding(graph_repo) -> bool:
    mode = os.getenv("GRAPHRAG_ENABLE_VECTOR_RETRIEVAL", "auto").strip().lower()
    if mode in {"1", "true", "yes", "on"}:
        return True
    if mode in {"0", "false", "no", "off"}:
        return False
    profile = getattr(graph_repo, "profile", lambda: {})()
    return profile.get("backend") == "neo4j"


def _question_embedding(question: str, graph_repo) -> list[float] | None:
    if not _should_use_question_embedding(graph_repo):
        return None
    try:
        return embed_text(question)
    except Exception:
        return None


def create_session(owner_id: str, title: str | None = None, actor_id: str | None = None) -> dict:
    now = _now()
    session = {
        "id": f"s_{uuid.uuid4().hex[:10]}",
        "owner_id": owner_id,
        "title": title or "新对话",
        "created_at": now,
        "updated_at": now,
        "messages": [],
        "last_question": "",
        "last_answer": "",
    }
    if actor_id:
        session["actor_id"] = actor_id
    app_store.get_app_repository().save_chat_session(session)
    return session


def get_sessions(owner_id: str, page: int = 1, page_size: int = 20) -> dict:
    page_size = min(page_size, 50)
    all_sessions = [
        _session_summary(session)
        for session in app_store.get_app_repository().list_chat_sessions(owner_id)
        if session.get("messages")
    ]
    total = len(all_sessions)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": all_sessions[start: start + page_size],
    }


def get_session(session_id: str, owner_id: str) -> dict | None:
    session = app_store.get_app_repository().get_chat_session(session_id, owner_id)
    if not session:
        return None
    detail = _session_summary(session)
    detail["messages"] = session.get("messages", [])
    return detail


def run_query(
    question: str,
    history: list[dict],
    owner_id: str,
    session_id: str | None = None,
    persist_session: bool = True,
    allowed_document_ids: set[str] | None = None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    from pipeline.qa_agent import run_qa

    app_repo = app_store.get_app_repository()
    session = app_repo.get_chat_session(session_id, owner_id) if session_id and persist_session else None
    if session_id and persist_session and not session:
        # Deliberately indistinguishable from a nonexistent ID.
        raise ValueError("SESSION_NOT_FOUND")

    graph_repo = graph_store.get_graph_repository()
    if allowed_document_ids is None:
        kg_export = graph_repo.export_kg()
    else:
        from services import kg_service

        kg_export = kg_service.export_kg(allowed_doc_ids=allowed_document_ids)
    nodes = kg_export.get("nodes", [])
    edges = kg_export.get("edges", [])

    if not nodes:
        raise ValueError("KG_EMPTY")

    qa_history = _session_history(session) if session else history

    start = time.time()
    embedding = _question_embedding(question, graph_repo)
    retrieval = getattr(graph_repo, "hybrid_retrieve", lambda *args, **kwargs: {"nodes": [], "edges": [], "chunks": []})(
        question,
        embedding=embedding,
    )
    if allowed_document_ids is not None:
        retrieval["nodes"] = [
            node for node in retrieval.get("nodes", [])
            if node.get("source_doc") in allowed_document_ids
        ]
        allowed_node_ids = {node.get("id") for node in nodes}
        retrieval["edges"] = [
            edge for edge in retrieval.get("edges", [])
            if edge.get("source") in allowed_node_ids and edge.get("target") in allowed_node_ids
        ]
        retrieval["chunks"] = [
            chunk for chunk in retrieval.get("chunks", [])
            if chunk.get("doc_id") in allowed_document_ids
        ]
    context_chunks = retrieval.get("chunks", [])
    if retrieval.get("nodes"):
        known_ids = {node.get("id") for node in nodes}
        nodes.extend([node for node in retrieval["nodes"] if node.get("id") not in known_ids])
    if retrieval.get("edges"):
        edges.extend(retrieval["edges"])
    if context_chunks:
        result = run_qa(question, qa_history, nodes, edges, context_chunks=context_chunks)
    else:
        result = run_qa(question, qa_history, nodes, edges)
    elapsed = round(time.time() - start, 2)

    query_id = f"q_{uuid.uuid4().hex[:10]}"
    now = _now()

    if persist_session and not session:
        session = create_session(owner_id, _session_title(question), actor_id=actor_id)

    record = {
        "id": query_id,
        "owner_id": owner_id,
        "question": question,
        "answer": result["answer"],
        "tool_calls": result["tool_calls"],
        "cited_nodes": result["cited_nodes"],
        "cited_chunks": result.get("cited_chunks", [chunk.get("chunk_id") for chunk in context_chunks if chunk.get("chunk_id")]),
        "duration_seconds": elapsed,
        "timestamp": now,
    }
    if actor_id:
        record["actor_id"] = actor_id

    if persist_session and session:
        record["session_id"] = session["id"]
        human_msg = _new_message("human", question)
        ai_msg = _new_message(
            "ai",
            result["answer"],
            query_id=query_id,
            tool_calls=result["tool_calls"],
            cited_nodes=result["cited_nodes"],
            cited_chunks=record["cited_chunks"],
            duration_seconds=elapsed,
        )
        session.setdefault("messages", []).extend([human_msg, ai_msg])
        session["updated_at"] = now
        session["last_question"] = question
        session["last_answer"] = result["answer"]
        app_repo.save_chat_session(session)
        record["session"] = _session_summary(session)

    app_repo.append_query_history(record)

    if actor_id:
        usage = result.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        estimated = False
        if input_tokens <= 0:
            history_chars = sum(len(str(item.get("content") or item.get("question") or "")) for item in qa_history)
            input_tokens = max(1, (len(question) + history_chars + 3) // 4)
            estimated = True
        if output_tokens <= 0:
            output_tokens = max(1, (len(str(result.get("answer") or "")) + 3) // 4)
            estimated = True
        input_price = float(os.getenv("LLM_INPUT_CNY_PER_1M_TOKENS", "0") or 0)
        output_price = float(os.getenv("LLM_OUTPUT_CNY_PER_1M_TOKENS", "0") or 0)
        cost_microcny = round(input_tokens * input_price + output_tokens * output_price)
        try:
            get_account_repository().record_usage(
                tenant_id=tenant_id or owner_id,
                user_id=actor_id,
                operation="qa",
                provider=LLM_PROVIDER,
                model=LLM_MODEL,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microcny=cost_microcny,
                request_id=get_request_id(),
                payload={"estimated_tokens": estimated, "duration_seconds": elapsed},
            )
        except Exception as exc:
            logger.error("Usage event persistence failed (%s)", type(exc).__name__)

    public_record = dict(record)
    public_record.pop("owner_id", None)
    public_record.pop("actor_id", None)
    return public_record


def result_to_stream_events(result: dict, chunk_size: int = 18):
    """Convert a completed QA result into frontend-friendly stream events.

    The project keeps hidden model reasoning private. These events expose only
    observable progress: graph tool calls and answer text deltas.
    """
    for tool_call in result.get("tool_calls", []):
        yield {"event": "tool_call", "data": tool_call}

    answer = result.get("answer", "")
    if answer:
        for i in range(0, len(answer), chunk_size):
            yield {"event": "answer_delta", "data": {"text": answer[i: i + chunk_size]}}

    yield {"event": "done", "data": result}


def get_history(owner_id: str, page: int = 1, page_size: int = 20) -> dict:
    all_records = app_store.get_app_repository().load_query_history(owner_id)
    total = len(all_records)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {key: value for key, value in record.items() if key not in {"owner_id", "actor_id"}}
            for record in all_records[start: start + page_size]
        ],
    }


def _use_background_batch_worker() -> bool:
    mode = os.getenv("BATCH_RUNNER_MODE", "auto").strip().lower()
    if mode in {"inline", "inline_on_poll", "poll", "serverless"}:
        return False
    if mode in {"thread", "threads", "background"}:
        return True
    return os.getenv("VERCEL") not in {"1", "true", "True"}


def _public_batch_result(meta: dict) -> dict:
    public = dict(meta)
    public.pop("owner_id", None)
    public.pop("actor_id", None)
    public.pop("tenant_id", None)
    public.pop("questions", None)
    public.pop("next_index", None)
    return public


def _save_cancelled_batch(batch_id: str, meta: dict) -> dict:
    meta["status"] = "cancelled"
    meta["cancel_requested"] = True
    meta["updated_at"] = _now()
    app_store.get_app_repository().save_batch_meta(batch_id, meta)
    return meta


def _append_batch_result(meta: dict, question: str, owner_id: str) -> None:
    try:
        usage_identity = {}
        if meta.get("actor_id"):
            usage_identity = {
                "actor_id": meta["actor_id"],
                "tenant_id": meta.get("tenant_id") or owner_id,
            }
        res = run_query(question, [], owner_id, persist_session=False, **usage_identity)
        meta["results"].append(res)
        meta["completed"] += 1
    except Exception as exc:
        logger.error("Batch QA item failed (%s)", type(exc).__name__)
        meta["failed"] += 1
        meta["results"].append({"question": question, "error": PUBLIC_QA_ERROR})


def _process_batch_items(
    batch_id: str,
    owner_id: str,
    max_items: int | None = None,
    initial_meta: dict | None = None,
) -> dict | None:
    app_repo = app_store.get_app_repository()
    meta = app_repo.load_batch_meta(batch_id, owner_id) or initial_meta
    if not meta:
        return None
    if meta.get("status") in {"done", "cancelled"}:
        return meta
    if meta.get("cancel_requested"):
        return _save_cancelled_batch(batch_id, meta)

    questions = meta.get("questions", [])
    if not questions:
        return meta

    meta["status"] = "running"
    meta["updated_at"] = _now()
    app_repo.save_batch_meta(batch_id, meta)

    processed = 0
    while int(meta.get("next_index", 0)) < len(questions):
        if max_items is not None and processed >= max_items:
            break

        latest = app_repo.load_batch_meta(batch_id, owner_id) or meta
        if latest.get("cancel_requested") or latest.get("status") == "cancelled":
            latest.update({
                "questions": questions,
                "next_index": meta.get("next_index", 0),
            })
            return _save_cancelled_batch(batch_id, latest)

        meta.update(latest)
        question = questions[int(meta.get("next_index", 0))]
        _append_batch_result(meta, question, owner_id)
        meta["next_index"] = int(meta.get("next_index", 0)) + 1
        meta["updated_at"] = _now()
        meta["status"] = "running"
        app_repo.save_batch_meta(batch_id, meta)
        processed += 1

    if int(meta.get("next_index", 0)) >= len(questions):
        meta["status"] = "done"
        meta["updated_at"] = _now()
        app_repo.save_batch_meta(batch_id, meta)

    return meta


def start_batch(
    questions: list[str],
    owner_id: str,
    *,
    actor_id: str | None = None,
    tenant_id: str | None = None,
) -> dict:
    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "batch_id": batch_id,
        "owner_id": owner_id,
        "actor_id": actor_id,
        "tenant_id": tenant_id or owner_id,
        "total": len(questions),
        "completed": 0,
        "failed": 0,
        "status": "submitted",
        "created_at": now,
        "updated_at": now,
        "cancel_requested": False,
        "questions": questions,
        "next_index": 0,
        "results": [],
    }
    app_store.get_app_repository().save_batch_meta(batch_id, meta)

    if _use_background_batch_worker():
        import threading
        threading.Thread(
            target=lambda: _process_batch_items(batch_id, owner_id, initial_meta=meta),
            daemon=True,
        ).start()

    return {"batch_id": batch_id, "total": len(questions), "status": "submitted", "created_at": now}


def get_batch_result(batch_id: str, owner_id: str) -> dict | None:
    meta = app_store.get_app_repository().load_batch_meta(batch_id, owner_id)
    if not meta:
        return None
    if not _use_background_batch_worker() and meta.get("status") in {"submitted", "running"}:
        meta = _process_batch_items(batch_id, owner_id, max_items=1) or meta
    return _public_batch_result(meta)


def _batch_summary(meta: dict) -> dict:
    return {
        "batch_id": meta.get("batch_id", ""),
        "total": meta.get("total", 0),
        "completed": meta.get("completed", 0),
        "failed": meta.get("failed", 0),
        "status": meta.get("status", "submitted"),
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", meta.get("created_at", "")),
        "cancel_requested": bool(meta.get("cancel_requested")),
    }


def list_batches(owner_id: str, page: int = 1, page_size: int = 20) -> dict:
    page = max(page, 1)
    page_size = min(max(page_size, 1), 50)
    batches = sorted(
        app_store.get_app_repository().list_batch_metas(owner_id),
        key=lambda item: item.get("updated_at") or item.get("created_at", ""),
        reverse=True,
    )
    total = len(batches)
    start = (page - 1) * page_size
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_batch_summary(item) for item in batches[start: start + page_size]],
    }


def cancel_batch(batch_id: str, owner_id: str) -> dict | None:
    app_repo = app_store.get_app_repository()
    meta = app_repo.load_batch_meta(batch_id, owner_id)
    if not meta:
        return None

    previous_status = meta.get("status", "submitted")
    if previous_status not in {"done", "cancelled"}:
        meta["status"] = "cancelled"
        meta["cancel_requested"] = True
        meta["updated_at"] = _now()
        app_repo.save_batch_meta(batch_id, meta)

    return {
        "batch_id": batch_id,
        "previous_status": previous_status,
        "status": meta.get("status"),
        "cancel_requested": bool(meta.get("cancel_requested")),
    }
