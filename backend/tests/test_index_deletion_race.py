from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class MemoryAppRepository:
    def __init__(self, meta: dict, document: dict | None = None):
        self.meta = dict(meta)
        self.document = dict(document) if document else None

    def list_all_jobs(self):
        return [dict(self.meta)]

    def load_job_meta(self, job_id):
        return dict(self.meta) if job_id == self.meta.get("job_id") else None

    def save_job_meta(self, job_id, meta):
        assert job_id == self.meta.get("job_id")
        self.meta = dict(meta)

    def get_document(self, doc_id):
        if self.document and doc_id == self.document.get("doc_id"):
            return dict(self.document)
        return None


def _active_meta(**overrides) -> dict:
    meta = {
        "job_id": "job_active",
        "doc_id": "doc_1",
        "status": "indexing",
        "stage": "Indexing",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target_engines": ["legacy", "lightrag"],
        "engines": {
            "legacy": {"status": "done", "job_id": "job_active"},
            "lightrag": {"status": "indexing", "job_id": "job_active"},
        },
    }
    meta.update(overrides)
    return meta


class IndexDeletionRaceTests(unittest.TestCase):
    def tearDown(self):
        from services import indexing_service as idx

        idx._cancel_flags.clear()

    def test_document_job_cancel_is_detached_and_survives_bulk_document_delete(self):
        from services import indexing_service as idx

        repo = MemoryAppRepository(
            _active_meta(),
            {"doc_id": "doc_1", "owner_id": "tenant_a", "indexes": {}},
        )
        with (
            patch.object(idx.app_store, "get_app_repository", return_value=repo),
            patch.object(idx.document_service, "update_engine_index_status"),
        ):
            cancelled = idx.cancel_document_jobs("doc_1", detach=True)

        self.assertEqual(cancelled, ["job_active"])
        self.assertEqual(repo.meta["status"], "cancelled")
        self.assertEqual(repo.meta["source_doc_id"], "doc_1")
        self.assertTrue(repo.meta["doc_id"].startswith("__cancelled_index__:doc_1:"))
        # The completed sibling remains discoverable by the outer deletion,
        # while the in-flight child is cancelled immediately.
        self.assertEqual(repo.meta["engines"]["legacy"]["status"], "done")
        self.assertEqual(repo.meta["engines"]["lightrag"]["status"], "cancelled")

    def test_user_cancel_cleans_completed_children_of_active_parent(self):
        from services import indexing_service as idx
        from services import lightrag_deletion_service

        meta = _active_meta(engines={
            "legacy": {"status": "done", "job_id": "job_active"},
            "lightrag": {"status": "done", "job_id": "job_active"},
        })
        document = {
            "doc_id": "doc_1",
            "owner_id": "tenant_a",
            "indexes": {
                "legacy": {"status": "done"},
                "lightrag": {"status": "done", "stats": {"page_ids": ["p1"]}},
            },
        }
        repo = MemoryAppRepository(meta, document)
        graph = Mock()
        graph.remove_document.return_value = (2, 1)

        with (
            patch.object(idx.app_store, "get_app_repository", return_value=repo),
            patch.object(idx.graph_store, "get_graph_repository", return_value=graph),
            patch.object(lightrag_deletion_service, "delete_or_schedule", return_value={"status": "queued"}) as delete_lightrag,
            patch.object(idx.document_service, "update_engine_index_status"),
        ):
            ok, previous = idx.cancel_job("job_active")

        self.assertTrue(ok)
        self.assertEqual(previous, "indexing")
        graph.remove_document.assert_called_once_with("doc_1")
        delete_lightrag.assert_called_once()
        self.assertEqual(repo.meta["engines"]["legacy"]["status"], "cancelled")
        self.assertEqual(repo.meta["engines"]["lightrag"]["status"], "cancelled")

    def test_legacy_publish_is_blocked_before_upsert_after_document_delete(self):
        from services import indexing_service as idx

        graph = Mock()
        page = SimpleNamespace(text="GraphRAG", page_idx=0)
        with (
            patch("pipeline.entity_extractor.create_model", return_value=object()),
            patch("pipeline.entity_extractor.extract_entities", return_value=SimpleNamespace(extractions=[])),
            patch("pipeline.kg_builder.extractions_to_records", return_value=[]),
            patch("pipeline.kg_builder.build_kg", return_value=([], [])),
            patch.object(idx, "_attach_embeddings"),
            patch.object(idx, "_save_job_artifact"),
            patch.object(idx, "_update_meta"),
            patch.object(idx, "_update_engine_meta"),
            patch.object(idx.document_service, "update_engine_index_status"),
            patch.object(idx.graph_store, "get_graph_repository", return_value=graph),
            patch.object(idx, "_assert_index_write_allowed", side_effect=idx._IndexCancelled("deleted")),
        ):
            with self.assertRaises(idx._IndexCancelled):
                idx._run_legacy_engine("job_active", {"doc_id": "doc_1"}, [], [page], {}, 0.0)

        graph.upsert_document_graph.assert_not_called()

    def test_legacy_publish_rolls_back_when_delete_races_with_upsert(self):
        from services import indexing_service as idx

        graph = Mock()
        page = SimpleNamespace(text="GraphRAG", page_idx=0)
        with (
            patch("pipeline.entity_extractor.create_model", return_value=object()),
            patch("pipeline.entity_extractor.extract_entities", return_value=SimpleNamespace(extractions=[])),
            patch("pipeline.kg_builder.extractions_to_records", return_value=[]),
            patch("pipeline.kg_builder.build_kg", return_value=([], [])),
            patch.object(idx, "_attach_embeddings"),
            patch.object(idx, "_save_job_artifact"),
            patch.object(idx, "_update_meta"),
            patch.object(idx, "_update_engine_meta"),
            patch.object(idx.document_service, "update_engine_index_status"),
            patch.object(idx.graph_store, "get_graph_repository", return_value=graph),
            patch.object(
                idx,
                "_assert_index_write_allowed",
                side_effect=[None, idx._IndexCancelled("deleted")],
            ),
        ):
            with self.assertRaises(idx._IndexCancelled):
                idx._run_legacy_engine("job_active", {"doc_id": "doc_1"}, [], [page], {}, 0.0)

        graph.upsert_document_graph.assert_called_once()
        graph.remove_document.assert_called_once_with("doc_1")

    def test_document_delete_cancels_active_jobs_before_removing_graph(self):
        from services import document_service as docs
        from services import indexing_service as idx
        from services import lightrag_deletion_service

        events: list[str] = []

        class Repo:
            def get_document(self, _doc_id):
                return {
                    "doc_id": "doc_1",
                    "owner_id": "tenant_a",
                    "indexes": {},
                    "blob_ref": {"key": "uploads/doc_1.pdf"},
                }

            def list_all_jobs(self):
                return []

            def delete_document(self, _doc_id):
                events.append("delete_row")
                return True

        graph = Mock()
        graph.remove_document.side_effect = lambda _doc_id: (events.append("remove_graph") or (0, 0))
        blob = Mock()

        with (
            patch.object(docs.app_store, "get_app_repository", return_value=Repo()),
            patch.object(docs.graph_store, "get_graph_repository", return_value=graph),
            patch.object(docs.blob_store, "get_blob_repository", return_value=blob),
            patch.object(idx, "cancel_document_jobs", side_effect=lambda *_args, **_kwargs: (events.append("cancel") or [])) as cancel,
            patch.object(lightrag_deletion_service, "delete_or_schedule", return_value={"status": "not_indexed"}),
        ):
            result = docs.delete_document("doc_1")

        self.assertEqual(result, (True, 0, 0))
        cancel.assert_called_once_with("doc_1", detach=True)
        self.assertEqual(events, ["cancel", "remove_graph", "delete_row"])

    def test_account_artifact_cleanup_detaches_jobs_before_graph_removal(self):
        from routers import account
        from services import indexing_service as idx
        from services import lightrag_deletion_service

        events: list[str] = []
        graph = Mock()
        graph.remove_document.side_effect = lambda _doc_id: events.append("remove_graph")
        blob = Mock()

        with (
            patch.object(account.graph_store, "get_graph_repository", return_value=graph),
            patch.object(account.blob_store, "get_blob_repository", return_value=blob),
            patch.object(idx, "cancel_document_jobs", side_effect=lambda *_args, **_kwargs: (events.append("cancel") or [])) as cancel,
            patch.object(lightrag_deletion_service, "delete_or_schedule", return_value={"status": "queued"}),
        ):
            account._remove_document_artifacts([{
                "doc_id": "doc_1",
                "owner_id": "tenant_a",
                "indexes": {"lightrag": {"status": "done"}},
            }])

        cancel.assert_called_once_with("doc_1", detach=True)
        self.assertEqual(events, ["cancel", "remove_graph"])

    def test_persisted_public_workspace_survives_environment_change_and_account_delete(self):
        from services import document_service as docs
        from services import lightrag_deletion_service

        document = {
            "doc_id": "public_doc",
            "owner_id": "tenant_a",
            "indexes": {"lightrag": {"status": "done", "stats": {"page_ids": ["p1"]}}},
        }

        class Repo:
            def get_document(self, _doc_id):
                return dict(document)

            def save_document(self, value):
                document.clear()
                document.update(value)

        with (
            patch.dict("os.environ", {"PUBLIC_DOCUMENT_IDS": "public_doc"}, clear=False),
            patch.object(docs.app_store, "get_app_repository", return_value=Repo()),
        ):
            self.assertEqual(
                docs.lightrag_tenant_for_document(document, persist=True),
                "public_demo",
            )

        self.assertEqual(document["lightrag_workspace_scope"], "public_demo")
        with patch.dict("os.environ", {"PUBLIC_DOCUMENT_IDS": ""}, clear=False):
            self.assertEqual(docs.lightrag_tenant_for_document(document), "public_demo")
            payload = lightrag_deletion_service._delete_payload(document)
        self.assertEqual(payload["tenant_id"], "public_demo")

    def test_cancelled_job_artifacts_are_erased_but_tombstone_is_retained(self):
        from services import indexing_service as idx

        meta = _active_meta(
            status="cancelled",
            doc_id="__cancelled_index__:doc_1:job_active",
            source_doc_id="doc_1",
            artifacts={"parsed_pages.json": {"key": "jobs/job_active/parsed_pages.json"}},
        )
        repo = MemoryAppRepository(meta)
        blob = Mock()
        with (
            patch.object(idx.app_store, "get_app_repository", return_value=repo),
            patch.object(idx.blob_store, "get_blob_repository", return_value=blob),
            patch.object(idx.fs, "delete_job") as delete_job,
        ):
            idx.purge_cancelled_job_artifacts(["job_active"])

        blob.delete.assert_called_once_with({"key": "jobs/job_active/parsed_pages.json"})
        delete_job.assert_called_once_with("job_active")
        self.assertEqual(repo.meta["artifacts"], {})
        self.assertEqual(repo.meta["source_doc_id"], "doc_1")
        self.assertEqual(repo.meta["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
