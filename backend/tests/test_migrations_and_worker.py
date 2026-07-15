from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class MigrationsAndWorkerTests(unittest.TestCase):
    def test_import_file_store_to_postgres_writes_business_records(self):
        from scripts import import_file_store_to_postgres as importer

        calls: list[tuple[str, str]] = []

        class StubAppRepo:
            def save_document(self, doc):
                calls.append(("doc", doc["doc_id"]))

            def save_job_meta(self, job_id, meta):
                calls.append(("job", job_id))

            def save_chat_session(self, session):
                calls.append(("session", session["id"]))

            def append_query_history(self, record):
                calls.append(("query", record["id"]))

            def save_batch_meta(self, batch_id, meta):
                calls.append(("batch", batch_id))

        with (
            patch.object(importer.fs, "load_docs_index", return_value={"doc_1": {"doc_id": "doc_1"}}),
            patch.object(importer.fs, "list_all_jobs", return_value=[{"job_id": "job_1"}]),
            patch.object(importer.fs, "list_chat_sessions", return_value=[{"id": "s_1"}]),
            patch.object(importer.fs, "load_query_history", return_value=[{"id": "q_1"}]),
            patch.object(importer.fs, "list_batch_metas", return_value=[{"batch_id": "b_1"}]),
            patch.object(importer.app_store, "get_app_repository", return_value=StubAppRepo()),
        ):
            result = importer.import_file_store_app_data(dry_run=False)

        self.assertEqual(result["documents"], 1)
        self.assertEqual(result["jobs"], 1)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["queries"], 1)
        self.assertEqual(result["batches"], 1)
        self.assertIn(("doc", "doc_1"), calls)
        self.assertIn(("batch", "b_1"), calls)

    def test_worker_processes_next_index_job_once(self):
        from scripts import worker

        processed = []

        with patch.object(worker.indexing_service, "process_next_index_job", side_effect=lambda timeout_seconds=5: processed.append(timeout_seconds) or {"job_id": "job_1"}):
            result = worker.run_worker_once(timeout_seconds=7)

        self.assertEqual(result["job_id"], "job_1")
        self.assertEqual(processed, [7])

    def test_documents_only_import_does_not_copy_private_runtime_history(self):
        from scripts import import_file_store_to_postgres as importer

        calls: list[tuple[str, str]] = []

        class StubAppRepo:
            def save_document(self, doc):
                calls.append(("doc", doc["doc_id"]))

            def __getattr__(self, name):
                raise AssertionError(f"documents-only import must not call {name}")

        with (
            patch.object(importer.fs, "load_docs_index", return_value={"doc_1": {"doc_id": "doc_1"}}),
            patch.object(importer.fs, "list_all_jobs", return_value=[{"job_id": "job_1"}]),
            patch.object(importer.fs, "list_chat_sessions", return_value=[{"id": "s_1"}]),
            patch.object(importer.fs, "load_query_history", return_value=[{"id": "q_1"}]),
            patch.object(importer.fs, "list_batch_metas", return_value=[{"batch_id": "b_1"}]),
            patch.object(importer.app_store, "get_app_repository", return_value=StubAppRepo()),
        ):
            result = importer.import_file_store_app_data(dry_run=False, documents_only=True)

        self.assertEqual(calls, [("doc", "doc_1")])
        self.assertEqual(result["imported"]["documents"], 1)
        self.assertEqual(result["imported"]["sessions"], 0)


if __name__ == "__main__":
    unittest.main()
