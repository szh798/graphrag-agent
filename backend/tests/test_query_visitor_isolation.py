from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import query as query_router
from services import qa_service as svc
from storage import app_repository
from storage import file_store as fs


VISITOR_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
VISITOR_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class StubGraphRepository:
    def export_kg(self):
        return {"nodes": [{"id": "n1", "name": "GraphRAG", "type": "TOPIC"}], "edges": []}

    def hybrid_retrieve(self, question, embedding=None):
        return {"nodes": [], "edges": [], "chunks": []}


def fake_run_qa(question, history, nodes, edges):
    return {
        "answer": f"answer for {question}",
        "tool_calls": [],
        "cited_nodes": [],
    }


class VisitorRepositoryIsolationTests(unittest.TestCase):
    def test_filesystem_repository_filters_and_protects_visitor_records(self):
        repo = app_repository.FileAppRepository()
        with tempfile.TemporaryDirectory() as tmp, patch.object(fs, "_BASE", Path(tmp)):
            repo.save_chat_session({"id": "s_a", "owner_id": VISITOR_A, "messages": []})
            repo.save_chat_session({"id": "s_b", "owner_id": VISITOR_B, "messages": []})
            repo.save_chat_session({"id": "s_a", "owner_id": VISITOR_B, "title": "overwrite"})

            self.assertEqual(repo.get_chat_session("s_a", VISITOR_A)["owner_id"], VISITOR_A)
            self.assertIsNone(repo.get_chat_session("s_a", VISITOR_B))
            self.assertEqual([s["id"] for s in repo.list_chat_sessions(VISITOR_A)], ["s_a"])
            self.assertNotEqual(repo.get_chat_session("s_a", VISITOR_A).get("title"), "overwrite")

            repo.append_query_history({"id": "q_a", "owner_id": VISITOR_A})
            repo.append_query_history({"id": "q_b", "owner_id": VISITOR_B})
            self.assertEqual([q["id"] for q in repo.load_query_history(VISITOR_A)], ["q_a"])

            repo.save_batch_meta("batch_a", {"batch_id": "batch_a", "owner_id": VISITOR_A})
            repo.save_batch_meta("batch_b", {"batch_id": "batch_b", "owner_id": VISITOR_B})
            repo.save_batch_meta("batch_a", {"batch_id": "batch_a", "owner_id": VISITOR_B})
            self.assertEqual(repo.load_batch_meta("batch_a", VISITOR_A)["owner_id"], VISITOR_A)
            self.assertIsNone(repo.load_batch_meta("batch_a", VISITOR_B))
            self.assertEqual([b["batch_id"] for b in repo.list_batch_metas(VISITOR_B)], ["batch_b"])

    def test_service_returns_not_found_for_cross_visitor_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fs, "_BASE", Path(tmp)),
                patch.object(svc.graph_store, "get_graph_repository", return_value=StubGraphRepository()),
                patch("pipeline.qa_agent.run_qa", side_effect=fake_run_qa),
                patch.dict("os.environ", {"GRAPHRAG_APP_BACKEND": "filesystem"}),
            ):
                app_repository.reset_app_repository_cache()
                result = svc.run_query("visitor A question", [], VISITOR_A)

                self.assertIsNone(svc.get_session(result["session_id"], VISITOR_B))
                with self.assertRaisesRegex(ValueError, "SESSION_NOT_FOUND"):
                    svc.run_query("steal session", [], VISITOR_B, session_id=result["session_id"])

                self.assertEqual(svc.get_history(VISITOR_A)["total"], 1)
                self.assertEqual(svc.get_history(VISITOR_B)["total"], 0)
                self.assertEqual(svc.get_sessions(VISITOR_A)["total"], 1)
                self.assertEqual(svc.get_sessions(VISITOR_B)["total"], 0)
                self.assertNotIn("owner_id", result)
                self.assertNotIn("owner_id", svc.get_history(VISITOR_A)["items"][0])

                with patch.dict("os.environ", {"BATCH_RUNNER_MODE": "inline"}):
                    batch_a = svc.start_batch(["q1"], VISITOR_A)
                    svc.start_batch(["q2"], VISITOR_B)
                    self.assertIsNone(svc.get_batch_result(batch_a["batch_id"], VISITOR_B))
                    self.assertIsNone(svc.cancel_batch(batch_a["batch_id"], VISITOR_B))
                    self.assertEqual(svc.list_batches(VISITOR_A)["total"], 1)
                    self.assertEqual(svc.list_batches(VISITOR_B)["total"], 1)

                app_repository.reset_app_repository_cache()


class VisitorRouteIsolationTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(query_router.router)
        self.client = TestClient(app)

    def test_production_requires_canonical_visitor_uuid(self):
        with patch.dict("os.environ", {"VERCEL": "1"}):
            missing = self.client.get("/query/history")
            invalid = self.client.get(
                "/query/history",
                headers={query_router.VISITOR_ID_HEADER: "not-a-uuid"},
            )
            uppercase = self.client.get(
                "/query/history",
                headers={query_router.VISITOR_ID_HEADER: VISITOR_A.upper()},
            )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(uppercase.status_code, 400)

    def test_session_id_is_404_for_a_different_visitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fs, "_BASE", Path(tmp)),
                patch.dict("os.environ", {"VERCEL": "1", "GRAPHRAG_APP_BACKEND": "filesystem"}),
            ):
                app_repository.reset_app_repository_cache()
                created = self.client.post(
                    "/query/sessions",
                    headers={query_router.VISITOR_ID_HEADER: VISITOR_A},
                )
                session_id = created.json()["data"]["id"]
                cross_owner = self.client.get(
                    f"/query/sessions/{session_id}",
                    headers={query_router.VISITOR_ID_HEADER: VISITOR_B},
                )
                own = self.client.get(
                    f"/query/sessions/{session_id}",
                    headers={query_router.VISITOR_ID_HEADER: VISITOR_A},
                )
                app_repository.reset_app_repository_cache()

        self.assertEqual(created.status_code, 200)
        self.assertEqual(cross_owner.status_code, 404)
        self.assertEqual(cross_owner.json()["code"], 2002)
        self.assertEqual(own.status_code, 200)

    def test_internal_stateless_batch_query_skips_session_but_normal_query_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(fs, "_BASE", Path(tmp)),
                patch.object(svc.graph_store, "get_graph_repository", return_value=StubGraphRepository()),
                patch("pipeline.qa_agent.run_qa", side_effect=fake_run_qa),
                patch.dict("os.environ", {"VERCEL": "1", "GRAPHRAG_APP_BACKEND": "filesystem"}),
            ):
                app_repository.reset_app_repository_cache()
                headers = {
                    query_router.VISITOR_ID_HEADER: VISITOR_A,
                    query_router.STATELESS_BATCH_HEADER: "1",
                }
                stateless = self.client.post(
                    "/query",
                    json={"question": "stateless question"},
                    headers=headers,
                )
                stateless_data = stateless.json()["data"]
                sessions_after_stateless = svc.get_sessions(VISITOR_A)

                normal = self.client.post(
                    "/query",
                    json={"question": "normal question"},
                    headers={query_router.VISITOR_ID_HEADER: VISITOR_A},
                )
                normal_data = normal.json()["data"]
                sessions_after_normal = svc.get_sessions(VISITOR_A)
                history = svc.get_history(VISITOR_A)
                app_repository.reset_app_repository_cache()

        self.assertEqual(stateless.status_code, 200)
        self.assertEqual(stateless.json()["code"], 0)
        self.assertEqual(stateless_data["question"], "stateless question")
        self.assertEqual(stateless_data["answer"], "answer for stateless question")
        self.assertIn("tool_calls", stateless_data)
        self.assertIn("cited_nodes", stateless_data)
        self.assertNotIn("session_id", stateless_data)
        self.assertNotIn("session", stateless_data)
        self.assertEqual(sessions_after_stateless["total"], 0)

        self.assertEqual(normal.status_code, 200)
        self.assertIn("session_id", normal_data)
        self.assertIn("session", normal_data)
        self.assertEqual(sessions_after_normal["total"], 1)
        self.assertEqual(history["total"], 2)

    def test_query_errors_do_not_expose_raw_exception_text(self):
        secret = "upstream secret API key leaked"
        with (
            patch.dict("os.environ", {"VERCEL": "1"}),
            patch.object(svc, "run_query", side_effect=RuntimeError(secret)),
        ):
            response = self.client.post(
                "/query",
                json={"question": "test"},
                headers={query_router.VISITOR_ID_HEADER: VISITOR_A},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["msg"], svc.PUBLIC_QA_ERROR)
        self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
