from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.runtime import create_internal_app  # noqa: E402
from lightrag_integration.adapters import RemoteLightRAGAdapter  # noqa: E402
from lightrag_integration.security import sign_request  # noqa: E402
from lightrag_integration.types import LightRAGMode  # noqa: E402


class RuntimeAdapter:
    def __init__(self):
        self.index_workspace = ""
        self.scoped_workspaces: list[str] = []

    async def health(self):
        return {"status": "ready", "target_version": "1.5.4"}

    async def index_pages(self, **kwargs):
        self.index_workspace = kwargs["workspace"]
        return {"status": "done", "page_ids": ["opaque-page"]}

    async def delete_document(self, **kwargs):
        return {"deleted": True}

    async def run_query(self, **kwargs):
        return {"answer": "ok", "references": []}

    async def stream_query(self, **kwargs):
        yield {"event": "answer_delta", "data": {"text": "native"}}
        yield {
            "event": "done",
            "data": {
                "answer": "native",
                "engine": "lightrag",
                "retrieval_mode": kwargs["mode"].value,
                "references": [],
                "cited_entities": [],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "model": "glm-query",
            },
        }

    async def stream_query_scopes(self, **kwargs):
        self.scoped_workspaces = list(kwargs["workspaces"])
        yield {"event": "answer_delta", "data": {"text": "one scoped answer"}}
        yield {
            "event": "done",
            "data": {
                "answer": "one scoped answer",
                "engine": "lightrag",
                "retrieval_mode": kwargs["mode"].value,
                "references": [
                    {"doc_id": "private", "page": 1, "chunk_id": "one"},
                    {"doc_id": "public", "page": 2, "chunk_id": "two"},
                ],
                "cited_entities": [],
                "usage": {"input_tokens": 2, "output_tokens": 1},
                "model": "glm-query",
                "workspace_scope_count": len(kwargs["workspaces"]),
            },
        }

    async def export_graph(self, **kwargs):
        return {"nodes": [], "edges": []}

    async def search_entities(self, **kwargs):
        return {"items": []}


class LightRAGRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.secret = "runtime-hmac-secret-" + "r" * 32
        self.env = patch.dict(os.environ, {"LIGHTRAG_HMAC_SECRET": self.secret}, clear=False)
        self.env.start()
        self.adapter = RuntimeAdapter()
        self.client = TestClient(create_internal_app(self.adapter))

    def tearDown(self):
        self.client.close()
        self.env.stop()

    def _headers(self, path: str, body: bytes, nonce: str):
        return {
            "Content-Type": "application/json",
            **sign_request("POST", path, body, secret=self.secret, nonce=nonce),
        }

    def test_unsigned_request_is_rejected(self):
        response = self.client.post("/internal/v1/query", json={
            "workspace": "ws_" + "a" * 40,
            "question": "hello",
        })
        self.assertEqual(response.status_code, 401)

    def test_live_probe_is_anonymous_and_minimal(self):
        response = self.client.get("/live")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "live", "version": "1.5.4"})

    def test_graph_endpoint_accepts_complete_export_caps(self):
        path = "/internal/v1/graph/export"
        payload = {
            "workspace": "ws_" + "d" * 40,
            "max_nodes": 10000,
            "max_edges": 100000,
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        response = self.client.post(
            path,
            content=body,
            headers=self._headers(path, body, "runtime-graph-nonce-001"),
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_signed_index_accepts_only_opaque_workspace(self):
        path = "/internal/v1/index"
        payload = {
            "workspace": "ws_" + "b" * 40,
            "doc_id": "doc-1",
            "filename": "notes.md",
            "pages": [{"page": 1, "content": "hello"}],
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        response = self.client.post(
            path,
            content=body,
            headers=self._headers(path, body, "runtime-index-nonce-001"),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.adapter.index_workspace, payload["workspace"])

        invalid = {**payload, "workspace": "raw-customer-tenant"}
        invalid_body = json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode()
        invalid_response = self.client.post(
            path,
            content=invalid_body,
            headers=self._headers(path, invalid_body, "runtime-index-nonce-002"),
        )
        self.assertEqual(invalid_response.status_code, 422)

    def test_signed_query_stream_preserves_native_sse_events(self):
        path = "/internal/v1/query/stream"
        payload = {
            "workspace": "ws_" + "a" * 40,
            "question": "hello",
            "retrieval_mode": "mix",
            "history": [],
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        response = self.client.post(
            path,
            content=body,
            headers=self._headers(path, body, "runtime-stream-nonce-001"),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: answer_delta", response.text)
        self.assertIn('"text": "native"', response.text)
        self.assertIn("event: done", response.text)
        self.assertIn('"model": "glm-query"', response.text)

    def test_internal_query_rejects_more_than_eight_history_items(self):
        path = "/internal/v1/query"
        payload = {
            "workspace": "ws_" + "b" * 40,
            "question": "hello",
            "history": [
                {"role": "user", "content": str(index)}
                for index in range(9)
            ],
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        response = self.client.post(
            path,
            content=body,
            headers=self._headers(path, body, "runtime-history-nonce-001"),
        )
        self.assertEqual(response.status_code, 422)

    def test_remote_adapter_parses_signed_internal_stream_incrementally(self):
        runtime = create_internal_app(self.adapter)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_client(
                *args,
                transport=httpx.ASGITransport(app=runtime),
                **kwargs,
            )

        async def collect():
            remote = RemoteLightRAGAdapter(
                base_url="http://testserver", secret=self.secret
            )
            events = []
            with patch("httpx.AsyncClient", side_effect=client_factory):
                async for event in remote.stream_query(
                    workspace="ws_" + "c" * 40,
                    question="hello",
                    mode=LightRAGMode.MIX,
                    history=[],
                    allowed_doc_ids=None,
                    include_references=True,
                ):
                    events.append(event)
            return events

        events = __import__("asyncio").run(collect())
        self.assertEqual(
            [event["event"] for event in events], ["answer_delta", "done"]
        )
        self.assertEqual(events[0]["data"]["text"], "native")
        self.assertEqual(events[-1]["data"]["model"], "glm-query")

    def test_remote_scoped_stream_sends_only_opaque_workspaces_and_one_answer(self):
        runtime = create_internal_app(self.adapter)
        real_client = httpx.AsyncClient

        def client_factory(*args, **kwargs):
            return real_client(
                *args,
                transport=httpx.ASGITransport(app=runtime),
                **kwargs,
            )

        workspaces = ["ws_" + "d" * 40, "ws_" + "e" * 40]

        async def collect():
            remote = RemoteLightRAGAdapter(
                base_url="http://testserver", secret=self.secret
            )
            events = []
            with patch("httpx.AsyncClient", side_effect=client_factory):
                async for event in remote.stream_query_scopes(
                    workspaces=workspaces,
                    question="compare",
                    mode=LightRAGMode.MIX,
                    history=[],
                    allowed_doc_ids={"private", "public"},
                    include_references=True,
                ):
                    events.append(event)
            return events

        events = __import__("asyncio").run(collect())
        self.assertEqual(self.adapter.scoped_workspaces, workspaces)
        self.assertEqual(
            [event["event"] for event in events], ["answer_delta", "done"]
        )
        self.assertEqual(events[0]["data"]["text"], "one scoped answer")
        self.assertEqual(len(events[-1]["data"]["references"]), 2)


if __name__ == "__main__":
    unittest.main()
