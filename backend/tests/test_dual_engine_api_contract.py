from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from routers import documents, indexing, kg, query, search  # noqa: E402
from models.schemas import QueryRequest as QueryRequestSchema  # noqa: E402


VISITOR_ID = "123e4567-e89b-42d3-a456-426614174000"
HEADERS = {"X-GraphRAG-Visitor-ID": VISITOR_ID}


def contract_client() -> TestClient:
    app = FastAPI()
    app.include_router(indexing.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(kg.router, prefix="/api/v1")
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(search.router, prefix="/api/v1")
    return TestClient(app)


class DualEngineApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = contract_client()

    def test_query_forwards_lightrag_engine_and_retrieval_mode(self):
        result = {
            "id": "q_contract",
            "question": "How are the concepts connected?",
            "answer": "By a tested relation.",
            "tool_calls": [],
            "cited_nodes": [],
            "cited_entities": [],
            "references": [],
            "duration_seconds": 0.1,
            "timestamp": "2026-07-20T00:00:00+00:00",
            "engine": "lightrag",
            "retrieval_mode": "hybrid",
        }
        with patch.object(query.svc, "run_query", return_value=result) as run_query:
            response = self.client.post(
                "/api/v1/query",
                headers=HEADERS,
                json={
                    "question": "How are the concepts connected?",
                    "history": [],
                    "engine": "lightrag",
                    "retrieval_mode": "hybrid",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["engine"], "lightrag")
        self.assertEqual(response.json()["data"]["retrieval_mode"], "hybrid")
        self.assertEqual(run_query.call_args.kwargs["engine"], "lightrag")
        self.assertEqual(run_query.call_args.kwargs["retrieval_mode"], "hybrid")
        self.assertEqual(run_query.call_args.kwargs["tenant_id"], VISITOR_ID)

    def test_upload_automatically_enqueues_one_dual_engine_parent_job(self):
        doc = {
            "doc_id": "doc_upload",
            "filename": "notes.md",
            "format": "md",
            "size_bytes": 4,
            "pages": 1,
            "uploaded_at": "2026-07-20T00:00:00+00:00",
            "status": "uploaded",
            "owner_id": VISITOR_ID,
        }
        job = {
            "job_id": "job_upload",
            "doc_id": "doc_upload",
            "status": "queued",
            "engines": {
                "legacy": {"status": "queued"},
                "lightrag": {"status": "queued"},
            },
            "target_engines": ["legacy", "lightrag"],
        }
        with (
            patch.object(
                documents,
                "_read_validated_upload",
                AsyncMock(return_value=(b"test", None)),
            ),
            patch.object(documents.svc, "save_upload", return_value=doc),
            patch.object(documents.svc, "get_document", return_value=doc),
            patch.object(documents.idx_svc, "start_indexing", return_value=job) as start,
        ):
            response = self.client.post(
                "/api/v1/documents/upload",
                headers=HEADERS,
                files={"file": ("notes.md", b"test", "text/markdown")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["job_id"], "job_upload")
        start.assert_called_once_with(
            "doc_upload",
            idempotency_key="automatic-upload:doc_upload",
        )

    def test_session_and_batch_pin_the_requested_engine_contract(self):
        session = {
            "id": "s_contract",
            "title": "New session",
            "created_at": "2026-07-20T00:00:00+00:00",
            "updated_at": "2026-07-20T00:00:00+00:00",
            "message_count": 0,
            "last_question": "",
            "last_answer": "",
            "engine": "lightrag",
            "retrieval_mode": "global",
            "messages": [],
        }
        batch = {
            "batch_id": "batch_contract",
            "total": 2,
            "status": "submitted",
            "created_at": "2026-07-20T00:00:00+00:00",
            "engine": "lightrag",
            "retrieval_mode": "naive",
        }
        with (
            patch.object(query.svc, "create_session", return_value=session) as create_session,
            patch.object(query.svc, "get_session", return_value=session),
            patch.object(query.svc, "start_batch", return_value=batch) as start_batch,
        ):
            session_response = self.client.post(
                "/api/v1/query/sessions",
                headers=HEADERS,
                json={"engine": "lightrag", "retrieval_mode": "global"},
            )
            batch_response = self.client.post(
                "/api/v1/query/batch",
                headers=HEADERS,
                json={
                    "questions": ["first", "second"],
                    "engine": "lightrag",
                    "retrieval_mode": "naive",
                },
            )

        self.assertEqual(session_response.status_code, 200)
        self.assertEqual(session_response.json()["data"]["retrieval_mode"], "global")
        self.assertEqual(create_session.call_args.kwargs["engine"], "lightrag")
        self.assertEqual(create_session.call_args.kwargs["retrieval_mode"], "global")
        self.assertEqual(batch_response.status_code, 202)
        self.assertEqual(batch_response.json()["data"]["retrieval_mode"], "naive")
        self.assertEqual(start_batch.call_args.kwargs["engine"], "lightrag")
        self.assertEqual(start_batch.call_args.kwargs["retrieval_mode"], "naive")

    def test_start_is_always_dual_while_graph_and_search_forward_engine(self):
        job = {
            "job_id": "job_contract",
            "doc_id": "doc_contract",
            "status": "queued",
            "stage": "Queued for worker",
            "created_at": "2026-07-20T00:00:00+00:00",
            "engines": {"lightrag": {"status": "queued"}},
        }
        graph = {"total": 1, "page": 1, "page_size": 50, "items": []}
        entities = {"query": "graph", "engine": "lightrag", "total": 0, "items": []}
        with (
            patch.object(
                indexing.doc_svc,
                "get_document",
                return_value={"doc_id": "doc_contract", "owner_id": VISITOR_ID},
            ),
            patch.object(indexing.idx_svc, "start_indexing", return_value=job) as start_indexing,
            patch.object(kg.svc, "get_nodes_for_engine", AsyncMock(return_value=graph)) as get_nodes,
            patch.object(
                search.svc,
                "search_entities_for_engine",
                AsyncMock(return_value=entities),
            ) as search_entities,
        ):
            index_response = self.client.post(
                "/api/v1/index/start",
                headers=HEADERS,
                json={"doc_id": "doc_contract", "engine": "lightrag"},
            )
            graph_response = self.client.get(
                "/api/v1/kg/nodes?engine=lightrag",
                headers=HEADERS,
            )
            search_response = self.client.get(
                "/api/v1/search/entities?q=graph&engine=lightrag",
                headers=HEADERS,
            )

        self.assertEqual(index_response.status_code, 202)
        start_indexing.assert_called_once_with("doc_contract")
        self.assertEqual(graph_response.status_code, 200)
        self.assertEqual(get_nodes.await_args.kwargs["engine"] if "engine" in get_nodes.await_args.kwargs else get_nodes.await_args.args[0], "lightrag")
        self.assertEqual(get_nodes.await_args.kwargs["tenant_id"], VISITOR_ID)
        self.assertEqual(search_response.status_code, 200)
        self.assertEqual(search_entities.await_args.args[0], "lightrag")
        self.assertEqual(search_entities.await_args.kwargs["tenant_id"], VISITOR_ID)

    def test_invalid_engine_and_mode_are_rejected_at_the_http_boundary(self):
        invalid_engine = self.client.post(
            "/api/v1/query",
            headers=HEADERS,
            json={"question": "test", "engine": "other"},
        )
        invalid_mode = self.client.post(
            "/api/v1/query",
            headers=HEADERS,
            json={
                "question": "test",
                "engine": "lightrag",
                "retrieval_mode": "other",
            },
        )

        self.assertEqual(invalid_engine.status_code, 422)
        self.assertEqual(invalid_mode.status_code, 422)

        compatible_history = QueryRequestSchema.model_validate(
            {
                "question": "test",
                "engine": "lightrag",
                "retrieval_mode": "mix",
                "history": [
                    {"role": "human", "content": str(index)}
                    for index in range(9)
                ],
            }
        )
        self.assertEqual(len(compatible_history.history), 9)

    def test_lightrag_stream_uses_native_deltas_without_completed_query_replay(self):
        async def native_stream(*args, **kwargs):
            yield {"event": "answer_delta", "data": {"text": "first"}}
            yield {"event": "answer_delta", "data": {"text": " second"}}
            yield {
                "event": "done",
                "data": {
                    "id": "q_stream",
                    "question": "stream it",
                    "answer": "first second",
                    "tool_calls": [],
                    "cited_nodes": [],
                    "cited_entities": [],
                    "references": [],
                    "duration_seconds": 0.1,
                    "timestamp": "2026-07-20T00:00:00+00:00",
                    "engine": "lightrag",
                    "retrieval_mode": "mix",
                    "model": "glm-query",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                },
            }

        with (
            patch.object(query.svc, "stream_lightrag_query", native_stream),
            patch.object(query.svc, "run_query") as completed_query,
        ):
            response = self.client.post(
                "/api/v1/query/stream",
                headers=HEADERS,
                json={
                    "question": "stream it",
                    "engine": "lightrag",
                    "retrieval_mode": "mix",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: answer_delta", response.text)
        self.assertIn('"text": "first"', response.text)
        self.assertIn('"text": " second"', response.text)
        self.assertEqual(response.text.count("event: done"), 1)
        completed_query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
