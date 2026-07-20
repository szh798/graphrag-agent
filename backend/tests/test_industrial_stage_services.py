from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class IndustrialStageServiceTests(unittest.TestCase):
    def test_document_upload_uses_app_and_blob_repositories(self):
        from services import document_service as svc

        saved_docs: list[dict] = []
        uploads: list[tuple[str, bytes]] = []

        class StubAppRepo:
            def save_document(self, doc):
                saved_docs.append(dict(doc))

        class StubBlobRepo:
            def save_upload(self, key, content, content_type=None):
                uploads.append((key, content))
                return {"key": key, "url": f"blob://{key}", "path": f"/tmp/{key}"}

        with (
            patch.object(svc.app_store, "get_app_repository", return_value=StubAppRepo()),
            patch.object(svc.blob_store, "get_blob_repository", return_value=StubBlobRepo()),
        ):
            doc = svc.save_upload("demo.pdf", b"PDF", language="ch")

        self.assertEqual(saved_docs[0]["doc_id"], doc["doc_id"])
        self.assertEqual(uploads[0][1], b"PDF")
        self.assertEqual(doc["blob_key"], uploads[0][0])
        self.assertEqual(doc["blob_url"], f"blob://{doc['blob_key']}")

    def test_indexing_queue_mode_enqueues_job_without_starting_local_thread(self):
        from services import indexing_service as svc

        docs = {
            "doc_1": {
                "doc_id": "doc_1",
                "filename": "demo.pdf",
                "upload_filename": "doc_1_demo.pdf",
                "blob_key": "uploads/doc_1_demo.pdf",
            }
        }
        saved_jobs: list[dict] = []
        queued_jobs: list[dict] = []
        document_statuses: list[tuple[str, str]] = []

        class StubAppRepo:
            def get_document(self, doc_id):
                return docs.get(doc_id)

            def save_job_meta(self, job_id, meta):
                saved_jobs.append(dict(meta))

            def update_document_status(self, doc_id, status, pages=None):
                document_statuses.append((doc_id, status))

        class StubQueueRepo:
            def is_durable(self):
                return True

            def enqueue_index_job(self, payload):
                queued_jobs.append(dict(payload))

        with (
            patch.object(svc.app_store, "get_app_repository", return_value=StubAppRepo()),
            patch.object(svc.queue_store, "get_queue_repository", return_value=StubQueueRepo()),
            patch.object(svc.threading, "Thread") as thread_cls,
        ):
            meta = svc.start_indexing("doc_1")

        self.assertEqual(meta["status"], "queued")
        self.assertEqual(saved_jobs[0]["status"], "queued")
        self.assertEqual(queued_jobs[0]["job_id"], meta["job_id"])
        self.assertEqual(document_statuses, [("doc_1", "indexing")])
        thread_cls.assert_not_called()

    def test_failed_durable_enqueue_is_terminal_and_retry_creates_a_fresh_job(self):
        from services import indexing_service as svc

        doc = {
            "doc_id": "doc_retry",
            "filename": "demo.pdf",
            "upload_filename": "doc_retry_demo.pdf",
            "owner_id": "tenant_1",
            "status": "uploaded",
        }
        jobs: dict[str, dict] = {}
        queue_calls: list[dict] = []

        class AppRepo:
            def get_document(self, doc_id):
                return dict(doc) if doc_id == doc["doc_id"] else None

            def save_document(self, value):
                doc.clear()
                doc.update(value)

            def save_job_meta(self, job_id, meta):
                jobs[job_id] = dict(meta)

            def load_job_meta(self, job_id):
                return dict(jobs[job_id]) if job_id in jobs else None

            def list_all_jobs(self):
                return [dict(item) for item in jobs.values()]

        class QueueRepo:
            def is_durable(self):
                return True

            def enqueue_index_job(self, payload):
                queue_calls.append(dict(payload))
                if len(queue_calls) == 1:
                    raise RuntimeError("redis unavailable")

        with (
            patch.dict("os.environ", {"LIGHTRAG_ENABLED": "true"}, clear=False),
            patch.object(svc.app_store, "get_app_repository", return_value=AppRepo()),
            patch.object(svc.queue_store, "get_queue_repository", return_value=QueueRepo()),
            patch.object(svc, "report_event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "redis unavailable"):
                svc.start_indexing("doc_retry")
            first_job_id = next(iter(jobs))
            second = svc.start_indexing("doc_retry")

        self.assertEqual(jobs[first_job_id]["status"], "failed")
        self.assertNotEqual(second["job_id"], first_job_id)
        self.assertEqual(queue_calls[-1]["job_id"], second["job_id"])

    def test_indexing_attaches_embeddings_for_neo4j_graph_backend(self):
        from services import indexing_service as svc

        nodes = [{"id": "n1", "name": "Python", "type": "TECHNOLOGY"}]
        chunks = [{"chunk_id": "c1", "text": "Python text"}]

        class StubGraphRepo:
            def profile(self):
                return {"backend": "neo4j"}

        with (
            patch.object(svc.graph_store, "get_graph_repository", return_value=StubGraphRepo()),
            patch.object(svc, "embed_texts", return_value=[[0.1], [0.2]]),
        ):
            svc._attach_embeddings(nodes, chunks)

        self.assertEqual(nodes[0]["embedding"], [0.1])
        self.assertEqual(chunks[0]["embedding"], [0.2])

    def test_qa_sessions_and_history_use_app_repository(self):
        from services import qa_service as svc

        owner_id = "11111111-1111-4111-8111-111111111111"
        saved_sessions: dict[str, dict] = {}
        history: list[dict] = []

        class StubAppRepo:
            def save_chat_session(self, session):
                saved_sessions[session["id"]] = dict(session)

            def get_chat_session(self, session_id, requested_owner_id):
                session = saved_sessions.get(session_id)
                if not session or session.get("owner_id") != requested_owner_id:
                    return None
                return session

            def list_chat_sessions(self, requested_owner_id):
                return [s for s in saved_sessions.values() if s.get("owner_id") == requested_owner_id]

            def append_query_history(self, record):
                history.append(dict(record))

        class StubGraphRepo:
            def export_kg(self):
                return {"nodes": [{"id": "n1", "name": "Neo4j", "type": "TECHNOLOGY"}], "edges": []}

            def hybrid_retrieve(self, question, embedding=None, limit=8, include_neighbors=True):
                return {"nodes": [], "edges": [], "chunks": []}

        def fake_run_qa(question, qa_history, nodes, edges):
            return {"answer": f"answer {question}", "tool_calls": [], "cited_nodes": []}

        with (
            patch.object(svc.app_store, "get_app_repository", return_value=StubAppRepo()),
            patch.object(svc.graph_store, "get_graph_repository", return_value=StubGraphRepo()),
            patch("pipeline.qa_agent.run_qa", side_effect=fake_run_qa),
        ):
            result = svc.run_query("hello", [], owner_id, session_id=None)

        self.assertIn(result["session_id"], saved_sessions)
        self.assertEqual(history[0]["id"], result["id"])

    def test_qa_uses_question_embedding_for_neo4j_hybrid_retrieval(self):
        from services import qa_service as svc

        owner_id = "11111111-1111-4111-8111-111111111111"
        embeddings_seen = []

        class StubAppRepo:
            def get_chat_session(self, session_id, requested_owner_id):
                return None

            def save_chat_session(self, session):
                pass

            def append_query_history(self, record):
                pass

        class StubGraphRepo:
            def profile(self):
                return {"backend": "neo4j"}

            def export_kg(self):
                return {"nodes": [{"id": "n1", "name": "Neo4j", "type": "TECHNOLOGY"}], "edges": []}

            def hybrid_retrieve(self, question, embedding=None, limit=8, include_neighbors=True):
                embeddings_seen.append(embedding)
                return {"nodes": [], "edges": [], "chunks": [{"chunk_id": "c1", "text": "chunk", "page": 1, "doc_id": "doc_1"}]}

        def fake_run_qa(question, qa_history, nodes, edges, context_chunks=None):
            return {"answer": "ok", "tool_calls": [], "cited_nodes": [], "cited_chunks": ["c1"]}

        with (
            patch.object(svc.app_store, "get_app_repository", return_value=StubAppRepo()),
            patch.object(svc.graph_store, "get_graph_repository", return_value=StubGraphRepo()),
            patch.object(svc, "embed_text", return_value=[0.1, 0.2, 0.3]),
            patch("pipeline.qa_agent.run_qa", side_effect=fake_run_qa),
        ):
            result = svc.run_query("Neo4j?", [], owner_id, persist_session=False)

        self.assertEqual(embeddings_seen, [[0.1, 0.2, 0.3]])
        self.assertEqual(result["cited_chunks"], ["c1"])

    def test_neo4j_hybrid_retrieve_returns_chunks_nodes_and_edges(self):
        from storage.graph_repository import Neo4jGraphRepository

        class FakeDriver:
            def __init__(self):
                self.calls: list[tuple[str, dict]] = []

            def execute_query(self, query, parameters_=None, **kwargs):
                self.calls.append((query, parameters_ or {}))
                if "vector.queryNodes" in query:
                    return [FakeRecord({"chunk": {"chunk_id": "c1", "text": "Python chunk", "doc_id": "doc_1", "page": 2, "score": 0.91}})], None, []
                if "fulltext.queryNodes" in query:
                    return [FakeRecord({"node": {"id": "n1", "name": "Python", "type": "TECHNOLOGY", "source_doc": "doc_1", "score": 3.0}})], None, []
                if "RELATED_TO" in query:
                    return [FakeRecord({"edge": {"source": "n1", "target": "n2", "relation": "CO_OCCURS_IN", "doc_id": "doc_1", "page": 2}})], None, []
                return [], None, []

            def verify_connectivity(self):
                pass

        class FakeRecord:
            def __init__(self, data):
                self._data = data

            def data(self):
                return self._data

        fake_driver = FakeDriver()

        with (
            patch.dict(sys.modules, {"neo4j": type("Neo4jModule", (), {"GraphDatabase": type("GraphDatabase", (), {"driver": staticmethod(lambda *a, **k: fake_driver)})})}),
            patch.dict("os.environ", {
                "NEO4J_URI": "neo4j+s://demo",
                "NEO4J_PASSWORD": "secret",
                "NEO4J_VECTOR_DIMENSIONS": "3",
            }, clear=False),
        ):
            repo = Neo4jGraphRepository()
            result = repo.hybrid_retrieve("Python", embedding=[0.1, 0.2, 0.3])

        self.assertEqual(result["chunks"][0]["chunk_id"], "c1")
        self.assertEqual(result["nodes"][0]["id"], "n1")
        self.assertEqual(result["edges"][0]["source"], "n1")


if __name__ == "__main__":
    unittest.main()
