from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.errors import LightRAGUnavailableError  # noqa: E402
from services import async_bridge, lightrag_deletion_service  # noqa: E402


class MemoryDeletionRepository:
    def __init__(self):
        self.meta = {
            "job_id": "delete_job_1",
            "job_type": "lightrag_delete",
            "doc_id": "__lightrag_delete__:doc_1",
            "source_doc_id": "doc_1",
            "status": "queued",
            "delete_payload": {
                "tenant_id": "tenant_a",
                "doc_id": "doc_1",
                "page_count": 2,
                "page_ids": ["page_1", "page_2"],
            },
        }
        self.saved_statuses: list[str] = []

    def load_job_meta(self, job_id: str):
        return dict(self.meta) if job_id == self.meta["job_id"] else None

    def save_job_meta(self, job_id: str, meta: dict):
        assert job_id == self.meta["job_id"]
        self.meta = dict(meta)
        self.saved_statuses.append(str(meta.get("status") or ""))


class LightRAGDeletionWorkerTests(unittest.TestCase):
    def tearDown(self):
        async_bridge.shutdown()

    def _assert_incomplete_result_is_retryable(self, result: dict) -> None:
        from services import lightrag_service

        repo = MemoryDeletionRepository()
        with (
            patch.object(lightrag_deletion_service.app_store, "get_app_repository", return_value=repo),
            patch.object(lightrag_service, "delete_document", new=AsyncMock(return_value=result)),
            patch.object(lightrag_deletion_service, "report_event") as report_event,
        ):
            with self.assertRaises(LightRAGUnavailableError):
                lightrag_deletion_service.run_delete_job("delete_job_1")

        self.assertEqual(repo.meta["status"], "queued")
        self.assertEqual(repo.meta["stage"], "LightRAG deletion queued for retry")
        self.assertNotEqual(repo.saved_statuses[-1], "done")
        self.assertNotIn("result", repo.meta)
        report_event.assert_called_once()

    def test_deleted_false_keeps_tombstone_queued(self):
        self._assert_incomplete_result_is_retryable({
            "deleted": False,
            "deleted_page_ids": ["page_1"],
            "failed_page_ids": [],
        })

    def test_nonempty_failed_page_ids_keeps_tombstone_queued(self):
        self._assert_incomplete_result_is_retryable({
            "deleted": True,
            "deleted_page_ids": ["page_1"],
            "failed_page_ids": ["page_2"],
        })

    def test_complete_delete_marks_tombstone_done(self):
        from services import lightrag_service

        repo = MemoryDeletionRepository()
        result = {
            "deleted": True,
            "deleted_page_ids": ["page_1", "page_2"],
            "failed_page_ids": [],
        }
        with (
            patch.object(lightrag_deletion_service.app_store, "get_app_repository", return_value=repo),
            patch.object(lightrag_service, "delete_document", new=AsyncMock(return_value=result)),
            patch.object(lightrag_deletion_service, "report_event") as report_event,
        ):
            completed = lightrag_deletion_service.run_delete_job("delete_job_1")

        self.assertEqual(completed["status"], "done")
        self.assertEqual(repo.meta["result"], result)
        report_event.assert_not_called()

    def test_partial_delete_does_not_ack_durable_queue_receipt(self):
        from services import indexing_service, lightrag_service

        repo = MemoryDeletionRepository()
        acknowledged: list[dict] = []

        class QueueRepository:
            def recover_expired_jobs(self):
                return {"recovered": [], "exhausted": []}

            def pop_index_job(self, _timeout_seconds: int):
                return {
                    "job_id": "delete_job_1",
                    "job_type": "lightrag_delete",
                    "_queue_receipt": "receipt-1",
                }

            def ack_index_job(self, payload: dict):
                acknowledged.append(dict(payload))

        with (
            patch.object(indexing_service.app_store, "get_app_repository", return_value=repo),
            patch.object(indexing_service.queue_store, "get_queue_repository", return_value=QueueRepository()),
            patch.object(
                lightrag_service,
                "delete_document",
                new=AsyncMock(return_value={
                    "deleted": False,
                    "deleted_page_ids": ["page_1"],
                    "failed_page_ids": ["page_2"],
                }),
            ),
            patch.object(lightrag_deletion_service, "report_event"),
        ):
            with self.assertRaises(LightRAGUnavailableError):
                indexing_service.process_next_index_job(1)

        self.assertEqual(acknowledged, [])
        self.assertEqual(repo.meta["status"], "queued")


if __name__ == "__main__":
    unittest.main()
