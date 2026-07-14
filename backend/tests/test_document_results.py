from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class DocumentResultTests(unittest.TestCase):
    def test_list_documents_exposes_upload_date_alias_for_frontend(self):
        from services import document_service as svc

        uploaded_at = "2026-06-30T00:00:00+00:00"

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(svc.fs, "_BASE", Path(tmp)):
                svc.fs.save_doc({
                    "doc_id": "doc_1",
                    "filename": "demo.pdf",
                    "format": "pdf",
                    "size_bytes": 128,
                    "pages": 3,
                    "uploaded_at": uploaded_at,
                    "status": "indexed",
                    "upload_filename": "doc_1_demo.pdf",
                })

                result = svc.list_documents(page=1, page_size=10)

        self.assertEqual(result["items"][0]["uploaded_at"], uploaded_at)
        self.assertEqual(result["items"][0]["upload_date"], uploaded_at)

    def test_document_index_result_returns_latest_done_job_for_document(self):
        from services import document_service as svc

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(svc.fs, "_BASE", Path(tmp)):
                svc.fs.save_doc({
                    "doc_id": "doc_1",
                    "filename": "demo.pdf",
                    "format": "pdf",
                    "size_bytes": 128,
                    "pages": 3,
                    "uploaded_at": "2026-06-30T00:00:00+00:00",
                    "status": "indexed",
                    "upload_filename": "doc_1_demo.pdf",
                })
                svc.fs.save_job_meta("job_old", {
                    "job_id": "job_old",
                    "doc_id": "doc_1",
                    "status": "done",
                    "created_at": "2026-06-29T00:00:00+00:00",
                    "elapsed_seconds": 4.0,
                })
                svc.fs.write_json(svc.fs.job_dir("job_old") / "stats.json", {"nodes": 1, "edges": 2, "pages": 1, "raw_extractions": 3})
                svc.fs.save_job_meta("job_new", {
                    "job_id": "job_new",
                    "doc_id": "doc_1",
                    "status": "done",
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "elapsed_seconds": 7.5,
                })
                svc.fs.write_json(svc.fs.job_dir("job_new") / "stats.json", {"nodes": 4, "edges": 5, "pages": 2, "raw_extractions": 6})
                svc.fs.write_json(svc.fs.job_dir("job_new") / "extractions.json", [{"text": "Python", "type": "TECHNOLOGY", "page": 1, "doc_id": "doc_1"}])
                svc.fs.write_json(svc.fs.job_dir("job_new") / "kg_nodes.json", [{"id": "n1", "name": "Python"}])
                svc.fs.write_json(svc.fs.job_dir("job_new") / "kg_edges.json", [{"source": "n1", "target": "n2"}])

                result = svc.get_document_index_result("doc_1")

        self.assertEqual(result["job_id"], "job_new")
        self.assertEqual(result["stats"]["nodes"], 4)
        self.assertEqual(result["summary"]["nodes"], 4)
        self.assertEqual(result["summary"]["edges"], 5)
        self.assertEqual(result["summary"]["extractions"], 6)

    def test_document_extractions_returns_latest_done_job_records(self):
        from services import document_service as svc

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(svc.fs, "_BASE", Path(tmp)):
                svc.fs.save_doc({
                    "doc_id": "doc_1",
                    "filename": "demo.pdf",
                    "format": "pdf",
                    "size_bytes": 128,
                    "pages": 3,
                    "uploaded_at": "2026-06-30T00:00:00+00:00",
                    "status": "indexed",
                    "upload_filename": "doc_1_demo.pdf",
                })
                svc.fs.save_job_meta("job_1", {
                    "job_id": "job_1",
                    "doc_id": "doc_1",
                    "status": "done",
                    "created_at": "2026-06-30T00:00:00+00:00",
                    "elapsed_seconds": 2.0,
                })
                svc.fs.write_json(svc.fs.job_dir("job_1") / "stats.json", {"nodes": 1, "edges": 0, "pages": 1, "raw_extractions": 1})
                svc.fs.write_json(svc.fs.job_dir("job_1") / "extractions.json", [
                    {"text": "Python", "type": "TECHNOLOGY", "page": 1, "alignment": "match_exact", "doc_id": "doc_1"}
                ])

                result = svc.get_document_extractions("doc_1")

        self.assertEqual(result["doc_id"], "doc_1")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["text"], "Python")


if __name__ == "__main__":
    unittest.main()
