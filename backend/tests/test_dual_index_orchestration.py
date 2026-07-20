from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class DualIndexOrchestrationTests(unittest.TestCase):
    def test_lightrag_only_retry_reuses_shared_pages_without_parsing_upload(self):
        from services import indexing_service as service

        meta = {
            "job_id": "job_retry",
            "doc_id": "doc_1",
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pdf_path": "/private/upload.pdf",
            "target_engines": ["lightrag"],
        }
        doc = {
            "doc_id": "doc_1",
            "filename": "upload.pdf",
            "owner_id": "tenant_1",
        }
        pages = [{"page": 1, "text": "Already parsed", "source_path": "upload.pdf#page=1"}]

        class Repo:
            def get_document(self, doc_id):
                return doc if doc_id == "doc_1" else None

        with (
            patch.object(service, "_load_job_meta", return_value=meta),
            patch.object(service.app_store, "get_app_repository", return_value=Repo()),
            patch.object(service, "_reusable_parsed_pages", return_value=pages),
            patch.object(service, "_run_lightrag_retry_from_artifact") as retry,
            patch.object(service, "_job_input_path") as input_path,
        ):
            service._run_pipeline("job_retry")

        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[2], pages)
        input_path.assert_not_called()

    def test_full_dual_job_does_not_use_an_old_parse_artifact_shortcut(self):
        from services import indexing_service as service

        meta = {
            "job_id": "job_both",
            "doc_id": "doc_1",
            "status": "queued",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pdf_path": "/private/upload.pdf",
            "target_engines": ["legacy", "lightrag"],
        }

        class Repo:
            def get_document(self, _doc_id):
                return {"doc_id": "doc_1", "filename": "upload.pdf"}

        with (
            patch.object(service, "_load_job_meta", return_value=meta),
            patch.object(service.app_store, "get_app_repository", return_value=Repo()),
            patch.object(service, "_reusable_parsed_pages") as reusable,
            patch.object(service, "_job_input_path", side_effect=RuntimeError("parse path reached")),
            patch.object(service, "_update_meta"),
            patch.object(service, "_update_engine_meta"),
            patch.object(service.document_service, "update_engine_index_status"),
            patch.object(service, "update_doc_status"),
        ):
            service._run_pipeline("job_both")

        reusable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
