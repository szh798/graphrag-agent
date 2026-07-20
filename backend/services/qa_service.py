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
from services import async_bridge


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
        "engine": session.get("engine", "legacy"),
        "retrieval_mode": session.get("retrieval_mode"),
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


def _normalize_engine_mode(engine: str, retrieval_mode: str | None) -> tuple[str, str | None]:
    if engine not in {"legacy", "lightrag"}:
        raise ValueError("INVALID_ENGINE")
    if engine == "legacy":
        return engine, None
    mode = retrieval_mode or "mix"
    if mode not in {"local", "global", "hybrid", "mix", "naive"}:
        raise ValueError("INVALID_RETRIEVAL_MODE")
    return engine, mode


def _public_demo_document_ids() -> set[str]:
    return {
        item.strip()
        for item in os.getenv("PUBLIC_DOCUMENT_IDS", "").split(",")
        if item.strip()
    }


def _references_from_chunks(chunks: list[dict]) -> list[dict]:
    repo = app_store.get_app_repository()
    load_index = getattr(repo, "load_documents_index", None)
    docs = load_index() if callable(load_index) else {}
    references: list[dict] = []
    seen: set[tuple] = set()
    for chunk in chunks:
        doc_id = str(chunk.get("doc_id") or "")
        page_raw = chunk.get("page")
        page = int(page_raw) + 1 if isinstance(page_raw, int) else None
        key = (doc_id, page, str(chunk.get("chunk_id") or ""))
        if key in seen:
            continue
        seen.add(key)
        text = str(chunk.get("text") or "").strip()
        references.append({
            "doc_id": doc_id,
            "filename": str((docs.get(doc_id) or {}).get("filename") or doc_id),
            "page": page,
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "excerpt": text[:280],
        })
    return references


def create_session(
    owner_id: str,
    title: str | None = None,
    actor_id: str | None = None,
    *,
    engine: str = "legacy",
    retrieval_mode: str | None = None,
) -> dict:
    engine, retrieval_mode = _normalize_engine_mode(engine, retrieval_mode)
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
        "engine": engine,
        "retrieval_mode": retrieval_mode,
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
    engine: str = "legacy",
    retrieval_mode: str | None = None,
) -> dict:
    engine, retrieval_mode = _normalize_engine_mode(engine, retrieval_mode)

    app_repo = app_store.get_app_repository()
    session = app_repo.get_chat_session(session_id, owner_id) if session_id and persist_session else None
    if session_id and persist_session and not session:
        # Deliberately indistinguishable from a nonexistent ID.
        raise ValueError("SESSION_NOT_FOUND")
    if session:
        session_engine, session_mode = _normalize_engine_mode(
            str(session.get("engine") or "legacy"),
            session.get("retrieval_mode"),
        )
        if session_engine != engine:
            raise ValueError("SESSION_ENGINE_MISMATCH")
        if session_engine == "lightrag" and session_mode != retrieval_mode:
            raise ValueError("SESSION_MODE_MISMATCH")

    qa_history = _session_history(session) if session else list(history or [])
    start = time.time()
    context_chunks: list[dict] = []

    if engine == "lightrag":
        from services import lightrag_service

        public_ids = _public_demo_document_ids()
        include_public = bool(allowed_document_ids is not None and allowed_document_ids & public_ids)
        result = async_bridge.run(lightrag_service.run_query(
            tenant_id=tenant_id or owner_id,
            question=question,
            mode=retrieval_mode or "mix",
            history=qa_history,
            allowed_doc_ids=allowed_document_ids,
            include_references=True,
            additional_tenants=["public_demo"] if include_public and (tenant_id or owner_id) != "public_demo" else None,
        ))
        result = {
            "answer": str(result.get("answer") or ""),
            "tool_calls": list(result.get("tool_calls") or []),
            "cited_nodes": list(result.get("cited_entities") or result.get("cited_nodes") or []),
            "cited_entities": list(result.get("cited_entities") or result.get("cited_nodes") or []),
            "references": list(result.get("references") or []),
            "cited_chunks": list(result.get("cited_chunks") or []),
            "usage": dict(result.get("usage") or {}),
            "model": result.get("model"),
            "provider": result.get("provider"),
        }
    else:
        from pipeline.qa_agent import run_qa

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
        result["references"] = _references_from_chunks(context_chunks)
        result["cited_entities"] = list(result.get("cited_nodes") or [])
        result["model"] = result.get("model") or LLM_MODEL
        result["provider"] = result.get("provider") or LLM_PROVIDER
    elapsed = round(time.time() - start, 2)
    return _persist_completed_query(
        question=question,
        result=result,
        owner_id=owner_id,
        session=session,
        persist_session=persist_session,
        actor_id=actor_id,
        tenant_id=tenant_id,
        engine=engine,
        retrieval_mode=retrieval_mode,
        qa_history=qa_history,
        context_chunks=context_chunks,
        elapsed=elapsed,
    )


def _persist_completed_query(
    *,
    question: str,
    result: dict,
    owner_id: str,
    session: dict | None,
    persist_session: bool,
    actor_id: str | None,
    tenant_id: str | None,
    engine: str,
    retrieval_mode: str | None,
    qa_history: list[dict],
    context_chunks: list[dict] | None = None,
    elapsed: float,
) -> dict:
    """Persist one completed query (streaming or non-streaming) exactly once."""

    context_chunks = context_chunks or []
    app_repo = app_store.get_app_repository()
    query_id = f"q_{uuid.uuid4().hex[:10]}"
    now = _now()

    if persist_session and not session:
        session = create_session(
            owner_id,
            _session_title(question),
            actor_id=actor_id,
            engine=engine,
            retrieval_mode=retrieval_mode,
        )

    record = {
        "id": query_id,
        "owner_id": owner_id,
        "question": question,
        "answer": str(result.get("answer") or ""),
        "tool_calls": list(result.get("tool_calls") or []),
        "cited_nodes": list(result.get("cited_nodes") or []),
        "cited_chunks": result.get(
            "cited_chunks",
            [
                chunk.get("chunk_id")
                for chunk in context_chunks
                if chunk.get("chunk_id")
            ],
        ),
        "cited_entities": list(
            result.get("cited_entities") or result.get("cited_nodes") or []
        ),
        "references": list(result.get("references") or []),
        "engine": engine,
        "retrieval_mode": retrieval_mode,
        "model": result.get("model") or LLM_MODEL,
        "provider": result.get("provider") or LLM_PROVIDER,
        "usage": dict(result.get("usage") or {}),
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
            record["answer"],
            query_id=query_id,
            tool_calls=record["tool_calls"],
            cited_nodes=record["cited_nodes"],
            cited_chunks=record["cited_chunks"],
            cited_entities=record["cited_entities"],
            references=record["references"],
            engine=engine,
            retrieval_mode=retrieval_mode,
            model=record["model"],
            provider=record["provider"],
            usage=record["usage"],
            duration_seconds=elapsed,
        )
        session.setdefault("messages", []).extend([human_msg, ai_msg])
        session["updated_at"] = now
        session["last_question"] = question
        session["last_answer"] = record["answer"]
        app_repo.save_chat_session(session)
        record["session"] = _session_summary(session)

    app_repo.append_query_history(record)

    if actor_id:
        usage = record["usage"]
        input_tokens = int(
            usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        )
        output_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        tracker_exact = usage.get("estimated") is False
        estimated = False
        # A TokenTracker-reported zero (for example a cache hit) is exact and
        # must stay zero.  Only engines without tracking receive an estimate.
        if input_tokens <= 0 and not tracker_exact:
            history_chars = sum(
                len(str(item.get("content") or item.get("question") or ""))
                for item in qa_history
            )
            input_tokens = max(1, (len(question) + history_chars + 3) // 4)
            estimated = True
        if output_tokens <= 0 and not tracker_exact:
            output_tokens = max(1, (len(record["answer"]) + 3) // 4)
            estimated = True
        input_price = float(os.getenv("LLM_INPUT_CNY_PER_1M_TOKENS", "0") or 0)
        output_price = float(os.getenv("LLM_OUTPUT_CNY_PER_1M_TOKENS", "0") or 0)
        cost_microcny = round(
            input_tokens * input_price + output_tokens * output_price
        )
        try:
            get_account_repository().record_usage(
                tenant_id=tenant_id or owner_id,
                user_id=actor_id,
                operation="qa",
                provider=str(record["provider"]),
                model=str(record["model"]),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microcny=cost_microcny,
                request_id=get_request_id(),
                payload={
                    "estimated_tokens": estimated,
                    "duration_seconds": elapsed,
                },
            )
        except Exception as exc:
            logger.error("Usage event persistence failed (%s)", type(exc).__name__)

    public_record = dict(record)
    public_record.pop("owner_id", None)
    public_record.pop("actor_id", None)
    return public_record


async def stream_lightrag_query(
    question: str,
    history: list[dict],
    owner_id: str,
    session_id: str | None = None,
    *,
    allowed_document_ids: set[str] | None = None,
    actor_id: str | None = None,
    tenant_id: str | None = None,
    retrieval_mode: str | None = None,
):
    """Yield native LightRAG deltas, then persist and return one final record."""

    engine, retrieval_mode = _normalize_engine_mode(
        "lightrag", retrieval_mode
    )
    app_repo = app_store.get_app_repository()
    session = (
        app_repo.get_chat_session(session_id, owner_id) if session_id else None
    )
    if session_id and not session:
        raise ValueError("SESSION_NOT_FOUND")
    if session:
        session_engine, session_mode = _normalize_engine_mode(
            str(session.get("engine") or "legacy"),
            session.get("retrieval_mode"),
        )
        if session_engine != engine:
            raise ValueError("SESSION_ENGINE_MISMATCH")
        if session_mode != retrieval_mode:
            raise ValueError("SESSION_MODE_MISMATCH")

    qa_history = _session_history(session) if session else list(history or [])
    from services import lightrag_service

    public_ids = _public_demo_document_ids()
    include_public = bool(
        allowed_document_ids is not None
        and allowed_document_ids.intersection(public_ids)
    )
    started = time.time()
    completed: dict | None = None
    async for event in lightrag_service.stream_query(
        tenant_id=tenant_id or owner_id,
        question=question,
        mode=retrieval_mode or "mix",
        history=qa_history,
        allowed_doc_ids=allowed_document_ids,
        include_references=True,
        additional_tenants=(
            ["public_demo"]
            if include_public and (tenant_id or owner_id) != "public_demo"
            else None
        ),
    ):
        if event.get("event") == "answer_delta":
            yield event
        elif event.get("event") == "done":
            data = event.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("LIGHTRAG_STREAM_PROTOCOL_ERROR")
            completed = data

    if completed is None:
        raise RuntimeError("LIGHTRAG_STREAM_PROTOCOL_ERROR")
    normalized = {
        "answer": str(completed.get("answer") or ""),
        "tool_calls": list(completed.get("tool_calls") or []),
        "cited_nodes": list(
            completed.get("cited_entities")
            or completed.get("cited_nodes")
            or []
        ),
        "cited_entities": list(
            completed.get("cited_entities")
            or completed.get("cited_nodes")
            or []
        ),
        "references": list(completed.get("references") or []),
        "cited_chunks": list(completed.get("cited_chunks") or []),
        "usage": dict(completed.get("usage") or {}),
        "model": completed.get("model"),
        "provider": completed.get("provider"),
    }
    final = _persist_completed_query(
        question=question,
        result=normalized,
        owner_id=owner_id,
        session=session,
        persist_session=True,
        actor_id=actor_id,
        tenant_id=tenant_id,
        engine=engine,
        retrieval_mode=retrieval_mode,
        qa_history=qa_history,
        elapsed=round(time.time() - started, 2),
    )
    yield {"event": "done", "data": final}


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
    public.pop("allowed_document_ids", None)
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
        query_options = dict(usage_identity)
        engine = str(meta.get("engine") or "legacy")
        if engine != "legacy" or meta.get("retrieval_mode") is not None:
            query_options.update({"engine": engine, "retrieval_mode": meta.get("retrieval_mode")})
        if meta.get("allowed_document_ids") is not None:
            query_options["allowed_document_ids"] = set(meta["allowed_document_ids"])
        res = run_query(question, [], owner_id, persist_session=False, **query_options)
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
    engine: str = "legacy",
    retrieval_mode: str | None = None,
    allowed_document_ids: set[str] | None = None,
) -> dict:
    engine, retrieval_mode = _normalize_engine_mode(engine, retrieval_mode)
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
        "engine": engine,
        "retrieval_mode": retrieval_mode,
        "allowed_document_ids": sorted(allowed_document_ids) if allowed_document_ids is not None else None,
    }
    app_store.get_app_repository().save_batch_meta(batch_id, meta)

    if _use_background_batch_worker():
        import threading
        threading.Thread(
            target=lambda: _process_batch_items(batch_id, owner_id, initial_meta=meta),
            daemon=True,
        ).start()

    return {
        "batch_id": batch_id,
        "total": len(questions),
        "status": "submitted",
        "created_at": now,
        "engine": engine,
        "retrieval_mode": retrieval_mode,
    }


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
        "engine": meta.get("engine", "legacy"),
        "retrieval_mode": meta.get("retrieval_mode"),
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
