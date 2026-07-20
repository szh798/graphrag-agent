from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.errors import (  # noqa: E402
    LightRAGDisabledError,
    LightRAGUnavailableError,
)
from services import lightrag_service  # noqa: E402


class FakeAdapter:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def health(self):
        return {"status": "ready"}

    async def index_pages(self, **kwargs):
        self.calls.append(("index", kwargs["workspace"]))
        return {"engine": "lightrag", "status": "done", "page_ids": ["page"]}

    async def delete_document(self, **kwargs):
        self.calls.append(("delete", kwargs["workspace"]))
        return {"deleted": True}

    async def run_query(self, **kwargs):
        workspace = kwargs["workspace"]
        self.calls.append(("query", workspace))
        return {
            "engine": "lightrag",
            "retrieval_mode": kwargs["mode"].value,
            "answer": f"answer-{workspace[-4:]}",
            "references": [{
                "doc_id": workspace[-4:],
                "page": 1,
                "chunk_id": workspace,
                "filename": "doc.md",
                "excerpt": "text",
            }],
            "cited_entities": [workspace[-4:]],
            "usage": {"total_tokens": 5},
            "model": "glm",
            "elapsed_seconds": 0.1,
        }

    async def stream_query(self, **kwargs):
        workspace = kwargs["workspace"]
        self.calls.append(("stream", workspace))
        answer = f"stream-{workspace[-4:]}"
        yield {"event": "answer_delta", "data": {"text": answer}}
        yield {
            "event": "done",
            "data": {
                "engine": "lightrag",
                "retrieval_mode": kwargs["mode"].value,
                "answer": answer,
                "references": [],
                "cited_entities": [],
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "estimated": False,
                },
                "model": "glm",
                "elapsed_seconds": 0.1,
            },
        }

    async def stream_query_scopes(self, **kwargs):
        workspaces = kwargs["workspaces"]
        for workspace in workspaces:
            self.calls.append(("retrieve", workspace))
        self.calls.append(("synthesize", workspaces[0]))
        yield {"event": "answer_delta", "data": {"text": "one answer"}}
        yield {
            "event": "done",
            "data": {
                "engine": "lightrag",
                "retrieval_mode": kwargs["mode"].value,
                "answer": "one answer",
                "references": [
                    {
                        "doc_id": f"doc-{index}",
                        "page": index,
                        "chunk_id": f"chunk-{index}",
                        "filename": f"doc-{index}.md",
                        "excerpt": f"evidence-{index}",
                    }
                    for index, _workspace in enumerate(workspaces, start=1)
                ],
                "cited_entities": [],
                "usage": {
                    "input_tokens": 8,
                    "output_tokens": 2,
                    "total_tokens": 10,
                    "estimated": False,
                },
                "model": "glm",
                "elapsed_seconds": 0.2,
            },
        }

    async def export_graph(self, **kwargs):
        workspace = kwargs["workspace"]
        self.calls.append(("graph", workspace))
        node_id = f"lightrag:node:{workspace[-4:]}"
        return {
            "nodes": [{"id": node_id, "name": workspace[-4:]}],
            "edges": [],
            "truncated": False,
        }

    async def search_entities(self, **kwargs):
        workspace = kwargs["workspace"]
        self.calls.append(("search", workspace))
        return {"items": [{"id": f"lightrag:node:{workspace[-4:]}", "name": kwargs["query"]}]}


class LightRAGServiceTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        lightrag_service._set_adapter_for_tests(self.adapter)
        self.env = patch.dict(os.environ, {
            "LIGHTRAG_ENABLED": "true",
            "LIGHTRAG_WORKSPACE_SECRET": "workspace-" + "w" * 32,
            "LIGHTRAG_HMAC_SECRET": "internal-" + "h" * 32,
            "LIGHTRAG_BASE_URL": "https://lightrag.internal",
            "LIGHTRAG_DEFAULT_MODE": "mix",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        lightrag_service._set_adapter_for_tests(None)

    def test_disabled_operation_never_falls_back(self):
        with patch.dict(os.environ, {"LIGHTRAG_ENABLED": "false"}, clear=False):
            with self.assertRaises(LightRAGDisabledError):
                asyncio.run(lightrag_service.run_query(tenant_id="one", question="hello"))
        self.assertEqual(self.adapter.calls, [])

    def test_additional_tenants_retrieve_separately_but_generate_once(self):
        result = asyncio.run(lightrag_service.run_query(
            tenant_id="private-tenant",
            additional_tenants=["public-demo"],
            question="compare",
            mode="global",
        ))
        calls = [workspace for name, workspace in self.adapter.calls if name == "retrieve"]
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertTrue(all(workspace.startswith("ws_") for workspace in calls))
        self.assertTrue(all("tenant" not in workspace and "public" not in workspace for workspace in calls))
        self.assertEqual(
            sum(name == "synthesize" for name, _workspace in self.adapter.calls),
            1,
        )
        self.assertFalse(any(name == "query" for name, _ in self.adapter.calls))
        self.assertEqual(result["workspace_scope_count"], 2)
        self.assertEqual(result["retrieval_mode"], "global")
        self.assertEqual(result["usage"]["total_tokens"], 10)
        self.assertEqual(result["answer"], "one answer")
        self.assertNotIn("---", result["answer"])
        self.assertEqual(
            [reference["doc_id"] for reference in result["references"]],
            ["doc-1", "doc-2"],
        )

    def test_explicit_empty_allowed_documents_short_circuits(self):
        result = asyncio.run(lightrag_service.search_entities(
            tenant_id="private-tenant",
            query="Alice",
            allowed_doc_ids=set(),
        ))
        self.assertEqual(result["items"], [])
        self.assertEqual(self.adapter.calls, [])

    def test_partial_delete_result_is_a_retryable_failure(self):
        for result in (
            {"deleted": False, "failed_page_ids": []},
            {"deleted": True, "failed_page_ids": ["page_2"]},
        ):
            with self.subTest(result=result):
                with patch.object(
                    self.adapter,
                    "delete_document",
                    new=AsyncMock(return_value=result),
                ):
                    with self.assertRaises(LightRAGUnavailableError):
                        asyncio.run(lightrag_service.delete_document(
                            tenant_id="private-tenant",
                            doc_id="doc_1",
                            page_ids=["page_1", "page_2"],
                        ))

    def test_stream_emits_native_deltas_and_one_merged_done_event(self):
        async def collect():
            events = []
            async for event in lightrag_service.stream_query(
                tenant_id="private-tenant",
                additional_tenants=["public-demo"],
                question="compare",
                mode="mix",
                history=[
                    {"role": "user", "content": str(index)}
                    for index in range(12)
                ],
            ):
                events.append(event)
            return events

        events = asyncio.run(collect())
        self.assertEqual(sum(item["event"] == "done" for item in events), 1)
        deltas = [
            item["data"]["text"]
            for item in events
            if item["event"] == "answer_delta"
        ]
        self.assertEqual(deltas, ["one answer"])
        self.assertEqual(events[-1]["data"]["workspace_scope_count"], 2)
        self.assertIs(events[-1]["data"]["usage"]["estimated"], False)
        self.assertEqual(
            sum(name == "synthesize" for name, _workspace in self.adapter.calls),
            1,
        )


if __name__ == "__main__":
    unittest.main()
