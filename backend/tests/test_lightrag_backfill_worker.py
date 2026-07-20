from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
import os
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class AllTenantBackfillTests(unittest.TestCase):
    def test_startup_backfill_is_durable_locked_and_idempotent_per_release(self):
        from scripts import lightrag_backfill_worker as worker

        states: dict[str, dict] = {}
        calls: list[tuple[str, set[str]]] = []

        class FakeAppRepo:
            def list_documents(self):
                return [{
                    "doc_id": "doc_a",
                    "owner_id": "tenant_a",
                    "status": "indexed",
                    "indexes": {
                        "legacy": {"status": "done"},
                        "lightrag": {"status": "missing"},
                    },
                }]

            def load_job_meta(self, job_id):
                return states.get(job_id)

            def save_job_meta(self, job_id, meta):
                states[job_id] = dict(meta)

        class FakeQueue:
            def is_durable(self):
                return True

            def acquire_index_owner_lock(self, owner_id, job_id):
                return True

            def release_index_owner_lock(self, owner_id, job_id):
                return True

        def enqueue(doc_id, engines):
            calls.append((doc_id, engines))
            return {"job_id": "job_backfill"}

        env = {
            "LIGHTRAG_BACKFILL_ON_START": "true",
            "LIGHTRAG_BACKFILL_ALL_TENANTS_ACK": "YES",
            "LIGHTRAG_BACKFILL_RELEASE_ID": "release-1",
            "LIGHTRAG_BACKFILL_BATCH_SIZE": "25",
        }
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(worker.app_store, "get_app_repository", return_value=FakeAppRepo()),
            patch.object(worker.queue_store, "get_queue_repository", return_value=FakeQueue()),
            patch.object(worker.indexing_service, "start_indexing", side_effect=enqueue),
        ):
            first = worker.run_startup_backfill()
            second = worker.run_startup_backfill()

        self.assertEqual(first["status"], "done")
        self.assertEqual(second["status"], "already_done")
        self.assertEqual(calls, [("doc_a", {"lightrag"})])
        self.assertNotIn("tenant_a", str(states))

    def test_plans_all_tenants_and_enqueues_only_lightrag(self):
        from scripts.lightrag_backfill_worker import backfill_all_tenants

        documents = [
            {
                "doc_id": "doc_a",
                "owner_id": "tenant_a",
                "status": "indexed",
                "indexes": {
                    "legacy": {"status": "done"},
                    "lightrag": {"status": "missing"},
                },
            },
            {
                "doc_id": "doc_b",
                "owner_id": "tenant_b",
                "status": "indexed",
                "indexes": {
                    "legacy": {"status": "done"},
                    "lightrag": {"status": "done"},
                },
            },
        ]
        calls: list[tuple[str, set[str]]] = []

        def enqueue(doc_id: str, engines: set[str]):
            calls.append((doc_id, engines))
            return {"job_id": "job_a"}

        report = backfill_all_tenants(documents, enqueue=enqueue)

        self.assertEqual(report["tenants_scanned"], 2)
        self.assertEqual(report["documents_enqueued"], 1)
        self.assertEqual(calls, [("doc_a", {"lightrag"})])
        self.assertNotIn("tenant_a", str(report["planned"]))

    def test_startup_backfill_persists_failed_state_when_maintenance_crashes(self):
        from scripts import lightrag_backfill_worker as worker

        states: dict[str, dict] = {}

        class FakeAppRepo:
            def list_documents(self):
                raise RuntimeError("database interrupted")

            def load_job_meta(self, job_id):
                return states.get(job_id)

            def save_job_meta(self, job_id, meta):
                states[job_id] = dict(meta)

        class FakeQueue:
            def is_durable(self):
                return True

            def acquire_index_owner_lock(self, _owner_id, _job_id):
                return True

            def release_index_owner_lock(self, _owner_id, _job_id):
                return True

        with (
            patch.dict(os.environ, {
                "LIGHTRAG_BACKFILL_ON_START": "true",
                "LIGHTRAG_BACKFILL_ALL_TENANTS_ACK": "YES",
                "LIGHTRAG_BACKFILL_RELEASE_ID": "crash-test",
            }, clear=False),
            patch.object(worker.app_store, "get_app_repository", return_value=FakeAppRepo()),
            patch.object(worker.queue_store, "get_queue_repository", return_value=FakeQueue()),
        ):
            with self.assertRaisesRegex(RuntimeError, "database interrupted"):
                worker.run_startup_backfill()

        state = next(iter(states.values()))
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
