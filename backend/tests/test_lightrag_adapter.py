from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.adapters import LocalLightRAGAdapter  # noqa: E402


class FakeDeleteResult:
    status = "success"


class FakeRAG:
    def __init__(self):
        self.inserted = None
        self.deleted: list[str] = []
        self.finalized = 0

    async def ainsert(self, texts, *, ids, file_paths):
        self.inserted = (texts, ids, file_paths)

    async def adelete_by_doc_id(self, doc_id):
        self.deleted.append(doc_id)
        return FakeDeleteResult()

    async def get_llm_queue_status(self):
        return {"query": {"queued": 2, "completed_total": 5}}

    async def get_embedding_queue_status(self):
        return {"queue_size": 1}

    async def get_rerank_queue_status(self):
        return {"pending": 0}

    async def finalize_storages(self):
        self.finalized += 1


class FakeQueryParam:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeTracker:
    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def get_usage(self):
        return {"input_tokens": 12, "output_tokens": 3}


class StreamingRAG:
    def __init__(self):
        self._studio_token_tracker = FakeTracker()
        self._studio_query_model = "glm-query"
        self.seen_history = []
        self.system_prompt = ""

    async def aquery_data(self, question, *, param):
        self.seen_history = param.conversation_history
        return {
            "status": "success",
            "data": {
                "chunks": [
                    {
                        "reference_id": "1",
                        "chunk_id": "allowed-chunk",
                        "file_path": "graphrag://document/doc-ok/2?filename=ok.md",
                        "content": "allowed evidence",
                    },
                    {
                        "reference_id": "2",
                        "chunk_id": "secret-chunk",
                        "file_path": "graphrag://document/doc-secret/1?filename=secret.md",
                        "content": "TOP SECRET",
                    },
                ],
                "references": [
                    {
                        "reference_id": "1",
                        "file_path": "graphrag://document/doc-ok/2?filename=ok.md",
                    },
                    {
                        "reference_id": "2",
                        "file_path": "graphrag://document/doc-secret/1?filename=secret.md",
                    },
                ],
                "entities": [{"description": "TOP SECRET aggregate"}],
                "relationships": [],
            },
        }

    async def aquery_llm(self, question, *, param, system_prompt=None):
        self.system_prompt = str(system_prompt or "")

        async def chunks():
            yield "native "
            yield "stream"

        return {
            "status": "success",
            "data": {},
            "llm_response": {
                "content": None,
                "response_iterator": chunks(),
                "is_streaming": True,
            },
        }


class ScopedStreamingRAG:
    def __init__(self, doc_id: str, filename: str, content: str):
        self.doc_id = doc_id
        self.filename = filename
        self.content = content
        self._studio_token_tracker = FakeTracker()
        self._studio_query_model = "glm-query"
        self.generation_calls = 0
        self.seen_history = []
        self.system_prompt = ""

    async def aquery_data(self, question, *, param):
        self.seen_history = list(param.conversation_history)
        path = (
            f"graphrag://document/{self.doc_id}/1?filename={self.filename}"
        )
        return {
            "status": "success",
            "data": {
                "chunks": [
                    {
                        "reference_id": "1",
                        "chunk_id": f"chunk-{self.doc_id}",
                        "file_path": path,
                        "content": self.content,
                    },
                    {
                        "reference_id": "9",
                        "chunk_id": f"secret-{self.doc_id}",
                        "file_path": (
                            "graphrag://document/not-allowed/1?filename=secret.md"
                        ),
                        "content": "CROSS TENANT SECRET",
                    },
                ],
                "references": [
                    {"reference_id": "1", "file_path": path},
                    {
                        "reference_id": "9",
                        "file_path": (
                            "graphrag://document/not-allowed/1?filename=secret.md"
                        ),
                    },
                ],
                "entities": [],
                "relationships": [],
            },
        }

    async def aquery_llm(self, question, *, param, system_prompt=None):
        self.generation_calls += 1
        self.system_prompt = str(system_prompt or "")

        async def chunks():
            yield "single "
            yield "answer"

        return {
            "llm_response": {
                "content": None,
                "response_iterator": chunks(),
                "is_streaming": True,
            }
        }

class LightRAGAdapterTests(unittest.TestCase):
    def test_merged_context_is_round_robin_deduplicated_and_bounded(self):
        def context(doc_id: str, chunks: list[tuple[str, str]]) -> dict:
            return {
                "data": {
                    "chunks": [
                        {
                            "reference_id": str(index),
                            "chunk_id": chunk_id,
                            "file_path": (
                                f"graphrag://document/{doc_id}/1?filename={doc_id}.md"
                            ),
                            "content": content,
                        }
                        for index, (chunk_id, content) in enumerate(chunks, start=1)
                    ],
                    "references": [],
                }
            }

        contexts = [
            context("private", [("same", "duplicate"), ("p2", "private-2")]),
            context("private", [("same", "duplicate"), ("u2", "public-2")]),
        ]
        with patch.dict(
            os.environ,
            {"LIGHTRAG_MERGED_CONTEXT_MAX_CHUNKS": "2"},
            clear=False,
        ):
            merged = LocalLightRAGAdapter._merge_query_contexts(
                contexts, include_references=True
            )

        chunks = merged["data"]["chunks"]
        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            [chunk["content"] for chunk in chunks],
            ["duplicate", "public-2"],
        )
        self.assertEqual(
            [chunk["reference_id"] for chunk in chunks], ["1", "2"]
        )

    def test_page_ids_are_stable_and_deletion_reuses_same_ids(self):
        fake = FakeRAG()
        adapter = LocalLightRAGAdapter(instance_factory=lambda workspace: fake)
        workspace = "ws_" + "c" * 40

        first = asyncio.run(adapter.index_pages(
            workspace=workspace,
            doc_id="doc-1",
            filename="file.md",
            pages=[{"page": 1, "content": "one"}, {"page": 2, "content": "two"}],
        ))
        second = asyncio.run(adapter.index_pages(
            workspace=workspace,
            doc_id="doc-1",
            filename="file.md",
            pages=[{"page": 1, "content": "one"}, {"page": 2, "content": "two"}],
        ))
        self.assertEqual(first["page_ids"], second["page_ids"])
        self.assertTrue(all(item.startswith("lrpg_") for item in first["page_ids"]))
        self.assertTrue(all("doc-1" not in item for item in first["page_ids"]))

        deleted = asyncio.run(adapter.delete_document(
            workspace=workspace,
            doc_id="doc-1",
            page_ids=first["page_ids"],
        ))
        self.assertTrue(deleted["deleted"])
        self.assertEqual(fake.deleted, first["page_ids"])
        self.assertTrue(all(path.startswith("graphrag://document/") for path in fake.inserted[2]))

    def test_default_factory_explicitly_injects_all_model_bindings(self):
        captured = {}

        class FakeCore:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            async def initialize_storages(self):
                captured["initialized"] = True

        root_module = types.ModuleType("lightrag")
        root_module.LightRAG = FakeCore
        utils_module = types.ModuleType("lightrag.utils")

        class FakeTokenTracker:
            def reset(self):
                pass

            def get_usage(self):
                return {"input_tokens": 0, "output_tokens": 0}

        utils_module.TokenTracker = FakeTokenTracker
        bindings = SimpleNamespace(
            settings=SimpleNamespace(
                extract_model="glm-extract", query_model="glm-query"
            ),
            llm_model_func=object(),
            embedding_func=object(),
            rerank_model_func=object(),
            role_llm_configs={"extract": {"func": object()}},
        )
        adapter = LocalLightRAGAdapter()
        with tempfile.TemporaryDirectory() as working_dir, patch.dict(
            sys.modules,
            {"lightrag": root_module, "lightrag.utils": utils_module},
        ), patch.object(adapter, "_check_distribution"), patch(
            "lightrag_integration.adapters.build_provider_bindings",
            return_value=bindings,
        ), patch.dict(
            os.environ, {"LIGHTRAG_WORKING_DIR": working_dir}, clear=False
        ):
            asyncio.run(adapter._default_factory("ws_" + "a" * 40))

        self.assertIs(captured["llm_model_func"], bindings.llm_model_func)
        self.assertIs(captured["embedding_func"], bindings.embedding_func)
        self.assertIs(captured["rerank_model_func"], bindings.rerank_model_func)
        self.assertEqual(
            set(captured["role_llm_configs"]), set(bindings.role_llm_configs)
        )
        self.assertIsInstance(
            captured["llm_model_kwargs"]["token_tracker"], FakeTokenTracker
        )
        self.assertIs(
            captured["role_llm_configs"]["extract"]["kwargs"]["token_tracker"],
            captured["llm_model_kwargs"]["token_tracker"],
        )
        self.assertFalse(captured["vlm_process_enable"])
        self.assertTrue(captured["initialized"])

    def test_health_is_fail_closed_componentized_and_secret_free(self):
        env = {
            "LIGHTRAG_STRICT_VERSION": "true",
            "LIGHTRAG_KV_STORAGE": "PGKVStorage",
            "LIGHTRAG_VECTOR_STORAGE": "PGVectorStorage",
            "LIGHTRAG_DOC_STATUS_STORAGE": "PGDocStatusStorage",
            "LIGHTRAG_GRAPH_STORAGE": "Neo4JStorage",
            "POSTGRES_HOST": "db.example",
            "POSTGRES_USER": "user",
            "POSTGRES_PASSWORD": "postgres-secret-value",
            "POSTGRES_DATABASE": "lightrag",
            "NEO4J_URI": "neo4j+s://graph.example",
            "NEO4J_USERNAME": "neo4j",
            "NEO4J_PASSWORD": "neo4j-secret-value",
            "LLM_API_KEY": "llm-secret-value",
            "LLM_BASE_URL": "https://glm.example/v1",
            "LLM_INDEX_MODEL": "glm-index",
            "LLM_MODEL": "glm-query",
            "LLM_EMBEDDING_MODEL": "embedding-3",
            "LLM_EMBEDDING_DIMENSIONS": "1024",
            "RERANK_BINDING_API_KEY": "rerank-secret-value",
            "RERANK_BINDING_HOST": "https://rerank.example/v1/rerank",
            "RERANK_MODEL": "BAAI/bge-reranker-v2-m3",
        }
        fake = FakeRAG()
        probe_calls = 0

        async def dependency_probe():
            nonlocal probe_calls
            probe_calls += 1
            return {
                "postgres": {"status": "ok", "reachable": True, "pgvector": True},
                "neo4j": {"status": "ok", "reachable": True},
                "llm": {"status": "ok", "reachable": True},
                "embedding": {
                    "status": "ok",
                    "reachable": True,
                    "dimensions": 1024,
                },
                "reranker": {"status": "ok", "reachable": True},
                "queue": {
                    "status": "ok",
                    "reachable": True,
                    "backend": "local_thread",
                    "durable": False,
                },
            }

        adapter = LocalLightRAGAdapter(
            instance_factory=lambda workspace: fake,
            dependency_probe=dependency_probe,
        )
        adapter._instances["ws_" + "e" * 40] = fake
        with patch.dict(os.environ, env, clear=True), patch(
            "lightrag_integration.adapters.installed_lightrag_version",
            return_value="1.5.4",
        ), patch(
            "lightrag_integration.adapters.lightrag_package_available",
            return_value=True,
        ):
            result = asyncio.run(adapter.health())
            cached = asyncio.run(adapter.health())

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["components"]["neo4j"]["status"], "ok")
        self.assertEqual(result["components"]["postgres"]["status"], "ok")
        self.assertEqual(result["components"]["reranker"]["status"], "ok")
        self.assertEqual(result["components"]["queue"]["status"], "ok")
        self.assertEqual(result["queue_depth"], 3)
        self.assertEqual(cached["status"], "ready")
        self.assertEqual(probe_calls, 1)
        encoded = json.dumps(result)
        self.assertNotIn("postgres-secret-value", encoded)
        self.assertNotIn("neo4j-secret-value", encoded)
        self.assertNotIn("llm-secret-value", encoded)
        self.assertNotIn("rerank-secret-value", encoded)

    def test_workspace_cache_evicts_lru_and_finalizes_storage(self):
        created: dict[str, FakeRAG] = {}

        def factory(workspace):
            created[workspace] = FakeRAG()
            return created[workspace]

        adapter = LocalLightRAGAdapter(instance_factory=factory)
        workspaces = ["ws_" + character * 40 for character in ("1", "2", "3")]

        async def exercise():
            for workspace in workspaces:
                await adapter._instance(workspace)

        with patch.dict(
            os.environ,
            {"LIGHTRAG_WORKSPACE_CACHE_MAX": "2"},
            clear=False,
        ):
            asyncio.run(exercise())

        self.assertEqual(set(adapter._instances), set(workspaces[1:]))
        self.assertEqual(created[workspaces[0]].finalized, 1)
        self.assertEqual(created[workspaces[1]].finalized, 0)

    def test_workspace_cache_does_not_evict_busy_instance(self):
        created: dict[str, FakeRAG] = {}

        def factory(workspace):
            created[workspace] = FakeRAG()
            return created[workspace]

        adapter = LocalLightRAGAdapter(instance_factory=factory)
        first = "ws_" + "a" * 40
        second = "ws_" + "b" * 40

        async def exercise():
            await adapter._instance(first)
            lock = adapter._usage_locks.setdefault(first, asyncio.Lock())
            await lock.acquire()
            try:
                await adapter._instance(second)
                self.assertEqual(set(adapter._instances), {first, second})
            finally:
                lock.release()
            await adapter._instance(second)

        with patch.dict(
            os.environ,
            {"LIGHTRAG_WORKSPACE_CACHE_MAX": "1"},
            clear=False,
        ):
            asyncio.run(exercise())

        self.assertEqual(set(adapter._instances), {second})
        self.assertEqual(created[first].finalized, 1)

    def test_workspace_cache_ttl_finalizes_an_idle_instance_once(self):
        workspace = "ws_" + "6" * 40
        fake = FakeRAG()
        adapter = LocalLightRAGAdapter(instance_factory=lambda _: fake)

        async def exercise():
            await adapter._instance(workspace)
            adapter._last_used[workspace] = time.monotonic() - 61
            await adapter._evict_instances()
            await adapter._evict_instances()

        with patch.dict(
            os.environ,
            {"LIGHTRAG_WORKSPACE_CACHE_TTL_SECONDS": "60"},
            clear=False,
        ):
            asyncio.run(exercise())

        self.assertNotIn(workspace, adapter._instances)
        self.assertEqual(fake.finalized, 1)

    def test_real_probe_failures_expose_only_error_type(self):
        adapter = LocalLightRAGAdapter(instance_factory=lambda workspace: FakeRAG())

        def failed_probe():
            raise RuntimeError("credential=must-never-leak")

        with patch.object(
            adapter, "_postgres_probe_sync", return_value={"reachable": True}
        ), patch.object(
            adapter, "_neo4j_probe_sync", return_value={"reachable": True}
        ), patch.object(
            adapter, "_llm_probe_sync", side_effect=failed_probe
        ), patch.object(
            adapter, "_embedding_probe_sync", return_value={"reachable": True}
        ), patch.object(
            adapter, "_reranker_probe_sync", return_value={"reachable": True}
        ), patch.object(
            adapter,
            "_queue_probe_sync",
            return_value={"reachable": True, "durable": True},
        ):
            result = asyncio.run(adapter._probe_dependencies())

        self.assertEqual(result["llm"]["status"], "error")
        self.assertEqual(result["llm"]["error_type"], "RuntimeError")
        self.assertNotIn("must-never-leak", json.dumps(result))

    def test_production_and_unknown_queues_fail_closed(self):
        with patch.dict(
            os.environ,
            {
                "ENVIRONMENT": "production",
                "GRAPHRAG_QUEUE_BACKEND": "local_thread",
            },
            clear=True,
        ):
            with self.assertRaises(Exception):
                LocalLightRAGAdapter._queue_probe_sync()

        with patch.dict(
            os.environ,
            {"GRAPHRAG_QUEUE_BACKEND": "typo_queue"},
            clear=True,
        ):
            with self.assertRaises(Exception):
                LocalLightRAGAdapter._queue_probe_sync()

    def test_metrics_snapshot_survives_concurrent_cache_eviction(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingMetricsRAG(FakeRAG):
            async def get_llm_queue_status(self):
                started.set()
                await release.wait()
                return {"queued": 1}

        first = "ws_" + "7" * 40
        second = "ws_" + "8" * 40
        created = {
            first: BlockingMetricsRAG(),
            second: FakeRAG(),
        }
        adapter = LocalLightRAGAdapter(
            instance_factory=lambda workspace: created[workspace]
        )

        async def exercise():
            await adapter._instance(first)
            metrics_task = asyncio.create_task(adapter._runtime_metrics())
            await started.wait()
            await adapter._instance(second)
            self.assertEqual(set(adapter._instances), {first, second})
            release.set()
            metrics, depth = await metrics_task
            return metrics, depth

        with patch.dict(
            os.environ,
            {"LIGHTRAG_WORKSPACE_CACHE_MAX": "1"},
            clear=False,
        ):
            metrics, depth = asyncio.run(exercise())

        self.assertEqual(depth, 2)
        self.assertTrue(metrics["llm_queues"])
        self.assertEqual(len(adapter._instances), 1)

    def test_waiting_same_workspace_keeps_one_lock_during_eviction(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        class BlockingInsertRAG(FakeRAG):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.active = 0
                self.max_active = 0

            async def ainsert(self, texts, *, ids, file_paths):
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    if self.calls == 1:
                        first_started.set()
                        await release_first.wait()
                    await asyncio.sleep(0)
                    self.inserted = (texts, ids, file_paths)
                finally:
                    self.active -= 1

        first = "ws_" + "9" * 40
        second = "ws_" + "0" * 40
        first_rag = BlockingInsertRAG()
        second_rag = FakeRAG()
        adapter = LocalLightRAGAdapter(
            instance_factory=lambda workspace: (
                first_rag if workspace == first else second_rag
            )
        )

        async def exercise():
            one = asyncio.create_task(
                adapter.index_pages(
                    workspace=first,
                    doc_id="doc-1",
                    filename="one.md",
                    pages=[{"page": 1, "content": "one"}],
                )
            )
            await first_started.wait()
            two = asyncio.create_task(
                adapter.index_pages(
                    workspace=first,
                    doc_id="doc-2",
                    filename="two.md",
                    pages=[{"page": 1, "content": "two"}],
                )
            )
            for _ in range(20):
                if adapter._active_workspaces.get(first) == 2:
                    break
                await asyncio.sleep(0)
            self.assertEqual(adapter._active_workspaces.get(first), 2)
            await adapter._instance(second)
            self.assertIn(first, adapter._instances)
            release_first.set()
            await asyncio.gather(one, two)

        with patch.dict(
            os.environ,
            {"LIGHTRAG_WORKSPACE_CACHE_MAX": "1"},
            clear=False,
        ):
            asyncio.run(exercise())

        self.assertEqual(first_rag.calls, 2)
        self.assertEqual(first_rag.max_active, 1)
        self.assertEqual(first_rag.finalized, 0)
        self.assertEqual(second_rag.finalized, 1)

    def test_scrub_metrics_drops_untrusted_string_details(self):
        scrubbed = LocalLightRAGAdapter._scrub_metrics(
            {
                "status": "error",
                "detail": "postgresql://user:secret@host/database",
                "nested": {"message": "Bearer secret-value", "queued": 2},
            }
        )
        encoded = json.dumps(scrubbed)
        self.assertIn('"status": "error"', encoded)
        self.assertIn('"queued": 2', encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("postgresql", encoded)

    def test_native_stream_filters_documents_before_synthesis(self):
        fake = StreamingRAG()
        adapter = LocalLightRAGAdapter(instance_factory=lambda workspace: fake)
        root_module = types.ModuleType("lightrag")
        root_module.QueryParam = FakeQueryParam

        async def collect():
            result = []
            async for event in adapter.stream_query(
                workspace="ws_" + "a" * 40,
                question="answer from selected docs",
                mode=__import__(
                    "lightrag_integration.types", fromlist=["LightRAGMode"]
                ).LightRAGMode.MIX,
                history=[
                    {"role": "user", "content": f"history-{index}"}
                    for index in range(12)
                ],
                allowed_doc_ids={"doc-ok"},
                include_references=True,
            ):
                result.append(event)
            return result

        with patch.dict(sys.modules, {"lightrag": root_module}):
            events = asyncio.run(collect())

        deltas = [
            item["data"]["text"]
            for item in events
            if item["event"] == "answer_delta"
        ]
        final = events[-1]["data"]
        self.assertEqual(deltas, ["native ", "stream"])
        self.assertEqual(final["answer"], "native stream")
        self.assertEqual(final["model"], "glm-query")
        self.assertEqual(final["usage"]["input_tokens"], 12)
        self.assertFalse(final["usage"]["estimated"])
        self.assertEqual([ref["doc_id"] for ref in final["references"]], ["doc-ok"])
        self.assertIn("allowed evidence", fake.system_prompt)
        self.assertNotIn("TOP SECRET", fake.system_prompt)
        self.assertEqual(len(fake.seen_history), 8)

    def test_multi_workspace_retrieves_twice_but_synthesizes_once(self):
        private = ScopedStreamingRAG(
            "doc-private", "private.md", "private evidence"
        )
        public = ScopedStreamingRAG(
            "doc-public", "public.md", "public evidence"
        )
        workspaces = ["ws_" + "1" * 40, "ws_" + "2" * 40]
        instances = {workspaces[0]: private, workspaces[1]: public}
        adapter = LocalLightRAGAdapter(
            instance_factory=lambda workspace: instances[workspace]
        )
        root_module = types.ModuleType("lightrag")
        root_module.QueryParam = FakeQueryParam

        async def collect():
            events = []
            async for event in adapter.stream_query_scopes(
                workspaces=workspaces,
                question="compare both sources",
                mode=__import__(
                    "lightrag_integration.types", fromlist=["LightRAGMode"]
                ).LightRAGMode.MIX,
                history=[
                    {"role": "user", "content": f"history-{index}"}
                    for index in range(12)
                ],
                allowed_doc_ids={"doc-private", "doc-public"},
                include_references=True,
            ):
                events.append(event)
            return events

        with patch.dict(sys.modules, {"lightrag": root_module}):
            events = asyncio.run(collect())

        self.assertEqual(
            [event["data"]["text"] for event in events[:-1]],
            ["single ", "answer"],
        )
        final = events[-1]["data"]
        self.assertEqual(final["answer"], "single answer")
        self.assertEqual(final["workspace_scope_count"], 2)
        self.assertEqual(
            [reference["doc_id"] for reference in final["references"]],
            ["doc-private", "doc-public"],
        )
        self.assertEqual(
            [reference["reference_id"] for reference in final["references"]],
            ["1", "2"],
        )
        self.assertEqual(private.generation_calls + public.generation_calls, 1)
        self.assertEqual(private.generation_calls, 1)
        self.assertIn("private evidence", private.system_prompt)
        self.assertIn("public evidence", private.system_prompt)
        self.assertNotIn("CROSS TENANT SECRET", private.system_prompt)
        self.assertEqual(len(private.seen_history), 8)
        self.assertEqual(len(public.seen_history), 8)


if __name__ == "__main__":
    unittest.main()
