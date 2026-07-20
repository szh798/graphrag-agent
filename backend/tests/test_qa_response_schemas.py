from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from models.schemas import BatchResultData, QAResult


class QAResponseSchemaTests(unittest.TestCase):
    def test_query_routes_publish_the_current_response_contract_in_openapi(self):
        from fastapi import FastAPI
        from routers.query import router

        app = FastAPI()
        app.include_router(router)
        components = app.openapi()["components"]["schemas"]
        query_fields = components["QAResult"]["properties"]
        tool_fields = components["ToolCallRecord"]["properties"]

        self.assertIn("id", query_fields)
        self.assertIn("duration_seconds", query_fields)
        self.assertIn("timestamp", query_fields)
        self.assertIn("references", query_fields)
        self.assertIn("usage", query_fields)
        self.assertNotIn("query_id", query_fields)
        self.assertEqual(
            {"step", "tool_name", "tool_input", "tool_output"},
            set(tool_fields),
        )

    def test_current_query_result_matches_service_and_frontend_field_names(self):
        result = QAResult.model_validate({
            "id": "q_current",
            "session_id": "s_1",
            "question": "What changed?",
            "answer": "The response contract changed.",
            "tool_calls": [{
                "step": 1,
                "tool_name": "hybrid_search",
                "tool_input": "{'query': 'contract'}",
                "tool_output": "one match",
            }],
            "cited_nodes": ["legacy:n1"],
            "cited_chunks": ["chunk-1"],
            "duration_seconds": 0.25,
            "timestamp": "2026-07-20T00:00:00+00:00",
            "engine": "lightrag",
            "retrieval_mode": "mix",
            "references": [{
                "doc_id": "doc-1",
                "filename": "contract.md",
                "page": 2,
                "chunk_id": "chunk-1",
                "excerpt": "response contract",
            }],
            "cited_entities": [
                "GraphRAG",
                {"id": "entity-1", "name": "LightRAG", "type": "TECHNOLOGY"},
            ],
            "model": "glm-test",
            "provider": "openai-compatible",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        })

        public = result.model_dump(exclude_none=True)
        self.assertEqual(public["id"], "q_current")
        self.assertEqual(public["tool_calls"][0]["tool_name"], "hybrid_search")
        self.assertEqual(public["duration_seconds"], 0.25)
        self.assertEqual(public["timestamp"], "2026-07-20T00:00:00+00:00")
        self.assertEqual(public["references"][0]["page"], 2)
        self.assertEqual(public["retrieval_mode"], "mix")
        self.assertNotIn("query_id", public)
        self.assertNotIn("elapsed_seconds", public)
        self.assertNotIn("created_at", public)

    def test_original_spec_names_remain_valid_input_aliases(self):
        result = QAResult.model_validate({
            "query_id": "q_legacy",
            "question": "legacy",
            "answer": "accepted",
            "tool_calls": [{
                "tool": "describe_graph",
                "input": {"node": "n1"},
                "output": "one node",
            }],
            "cited_nodes": [],
            "elapsed_seconds": 1.5,
            "created_at": "2026-03-05T10:30:00Z",
        })

        self.assertEqual(result.id, "q_legacy")
        self.assertEqual(result.query_id, "q_legacy")
        self.assertEqual(result.duration_seconds, 1.5)
        self.assertEqual(result.elapsed_seconds, 1.5)
        self.assertEqual(result.timestamp, "2026-03-05T10:30:00Z")
        self.assertEqual(result.created_at, "2026-03-05T10:30:00Z")
        self.assertEqual(result.tool_calls[0].tool_name, "describe_graph")
        self.assertEqual(result.tool_calls[0].tool_input, '{"node": "n1"}')
        self.assertEqual(result.tool_calls[0].tool, "describe_graph")

    def test_batch_contract_accepts_success_and_failure_items(self):
        batch = BatchResultData.model_validate({
            "batch_id": "batch-1",
            "total": 2,
            "completed": 1,
            "failed": 1,
            "status": "done",
            "created_at": "2026-07-20T00:00:00+00:00",
            "updated_at": "2026-07-20T00:00:01+00:00",
            "cancel_requested": False,
            "engine": "lightrag",
            "retrieval_mode": "naive",
            "results": [
                {
                    "id": "q_1",
                    "question": "works",
                    "answer": "yes",
                    "duration_seconds": 0.2,
                    "timestamp": "2026-07-20T00:00:01+00:00",
                    "engine": "lightrag",
                    "retrieval_mode": "naive",
                },
                {"question": "fails", "error": "QA service is temporarily unavailable."},
            ],
        })

        self.assertEqual(batch.engine, "lightrag")
        self.assertEqual(batch.retrieval_mode, "naive")
        self.assertEqual(batch.results[0].answer, "yes")
        self.assertIsNone(batch.results[1].answer)
        self.assertIsNotNone(batch.results[1].error)


if __name__ == "__main__":
    unittest.main()
