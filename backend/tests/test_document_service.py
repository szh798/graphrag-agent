from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class DocumentServiceTests(unittest.TestCase):
    def test_validate_upload_accepts_text_and_markdown_for_offline_parser(self):
        from services.document_service import validate_upload

        for filename in ("notes.txt", "demo.md", "guide.markdown"):
            with self.subTest(filename=filename):
                ok, code, message = validate_upload(filename, 128)
                self.assertTrue(ok, message)
                self.assertEqual(code, 0)

    def test_upload_limit_remains_exactly_200_mb(self):
        from services import document_service as svc

        self.assertEqual(svc.MAX_FILE_SIZE_MB, 200)
        self.assertTrue(svc.validate_upload("demo.pdf", 200 * 1024 * 1024)[0])
        ok, code, _ = svc.validate_upload("demo.pdf", 200 * 1024 * 1024 + 1)
        self.assertFalse(ok)
        self.assertEqual(code, 1003)

    def test_public_document_strips_blob_and_storage_references(self):
        from services.document_service import public_document

        result = public_document({
            "doc_id": "doc_1",
            "filename": "demo.pdf",
            "upload_filename": "doc_1_demo.pdf",
            "blob_key": "uploads/doc_1_demo.pdf",
            "blob_url": "https://blob.example/private",
            "blob_ref": {"path": "/tmp/private"},
        })

        self.assertEqual(result["doc_id"], "doc_1")
        for key in ("upload_filename", "blob_key", "blob_url", "blob_ref"):
            self.assertNotIn(key, result)

    def test_public_document_repairs_missing_markdown_page_count(self):
        from services.document_service import public_document

        result = public_document({
            "doc_id": "doc_markdown",
            "filename": "architecture.md",
            "format": "md",
            "pages": None,
            "status": "indexed",
        })

        self.assertEqual(result["pages"], 1)

    def test_public_document_normalizes_internal_job_states(self):
        from services.document_service import public_document

        expected = {
            "submitted": "indexing",
            "queued": "indexing",
            "parsing": "indexing",
            "extracting": "indexing",
            "done": "indexed",
            "cancelled": "uploaded",
            "unexpected": "unknown",
        }
        for internal_status, public_status in expected.items():
            with self.subTest(internal_status=internal_status):
                result = public_document({"filename": "demo.pdf", "status": internal_status})
                self.assertEqual(result["status"], public_status)

    def test_new_markdown_upload_starts_with_one_logical_page(self):
        from services import document_service as svc

        saved = []

        class FakeRepo:
            def save_document(self, doc):
                saved.append(doc)

        with patch.object(svc.app_store, "get_app_repository", return_value=FakeRepo()):
            doc = svc._save_document_record(
                doc_id="doc_markdown",
                filename="architecture.md",
                size_bytes=128,
                language="ch",
                enable_formula=True,
                enable_table=True,
                upload_filename="doc_markdown_architecture.md",
                blob_ref={"key": "uploads/doc_markdown_architecture.md"},
            )

        self.assertEqual(doc["pages"], 1)
        self.assertEqual(saved[0]["pages"], 1)

    def test_list_documents_restores_active_job_id_after_page_reload(self):
        from services import document_service as svc

        class FakeRepo:
            def list_documents(self):
                return [{
                    "doc_id": "doc_active",
                    "filename": "resume.jpg",
                    "format": "jpg",
                    "status": "queued",
                    "uploaded_at": "2026-07-17T00:00:00+00:00",
                }]

            def list_all_jobs(self):
                return [
                    {
                        "job_id": "job_active",
                        "doc_id": "doc_active",
                        "status": "queued",
                        "created_at": "2026-07-17T00:01:00+00:00",
                    },
                    {
                        "job_id": "job_old",
                        "doc_id": "doc_active",
                        "status": "failed",
                        "created_at": "2026-07-16T00:01:00+00:00",
                    },
                ]

        with patch.object(svc.app_store, "get_app_repository", return_value=FakeRepo()):
            result = svc.list_documents()

        self.assertEqual(result["items"][0]["status"], "indexing")
        self.assertEqual(result["items"][0]["job_id"], "job_active")

    def test_upload_content_checks_mime_and_magic(self):
        from services.document_service import detect_supported_image_format, validate_upload_content

        valid_pdf = validate_upload_content("demo.pdf", "application/pdf", b"%PDF-1.7\n", 9)
        wrong_magic = validate_upload_content("demo.pdf", "application/pdf", b"not a pdf", 9)
        wrong_mime = validate_upload_content("demo.png", "text/plain", b"\x89PNG\r\n\x1a\n", 8)

        self.assertTrue(valid_pdf[0])
        self.assertEqual(wrong_magic[1], 1002)
        self.assertEqual(wrong_mime[1], 1002)
        self.assertEqual(detect_supported_image_format(b"\x89PNG\r\n\x1a\nrest"), ("png", "image/png"))
        self.assertEqual(detect_supported_image_format(b"\xff\xd8\xffrest"), ("jpg", "image/jpeg"))
        self.assertIsNone(detect_supported_image_format(b"not-an-image"))

    def test_upload_reader_stops_when_stream_exceeds_limit(self):
        from routers import documents

        class FakeUpload:
            filename = "notes.txt"
            content_type = "text/plain"
            size = None

            def __init__(self):
                self.chunks = [b"abc", b"def", b"should-not-be-read"]
                self.reads = 0
                self.closed = False

            async def read(self, size=-1):
                self.reads += 1
                return self.chunks.pop(0) if self.chunks else b""

            async def close(self):
                self.closed = True

        upload = FakeUpload()
        with patch.object(documents.svc, "MAX_FILE_SIZE_BYTES", 5):
            content, error = __import__("asyncio").run(documents._read_validated_upload(upload))

        self.assertIsNone(content)
        self.assertEqual(error.status_code, 400)
        self.assertEqual(upload.reads, 2)
        self.assertTrue(upload.closed)

    def test_completed_direct_upload_registers_private_blob_without_file_bytes(self):
        from services import document_service as svc

        saved = []

        class FakeRepo:
            def save_document(self, doc):
                saved.append(doc)

        with patch.object(svc.app_store, "get_app_repository", return_value=FakeRepo()):
            doc = svc.register_direct_upload(
                filename="large.pdf",
                size_bytes=200 * 1024 * 1024,
                content_type="application/pdf",
                blob_ref={
                    "url": "https://store.private.blob.vercel-storage.com/uploads/large-random.pdf",
                    "downloadUrl": "https://store.private.blob.vercel-storage.com/uploads/large-random.pdf?download=1",
                    "pathname": "uploads/large-random.pdf",
                },
            )

        self.assertEqual(doc["size_bytes"], 200 * 1024 * 1024)
        self.assertEqual(doc["blob_key"], "uploads/large-random.pdf")
        self.assertEqual(saved[0]["doc_id"], doc["doc_id"])

    def test_completed_direct_upload_rejects_untrusted_blob_host(self):
        from services import document_service as svc

        with self.assertRaisesRegex(ValueError, "Invalid Blob storage URL"):
            svc.register_direct_upload(
                filename="demo.pdf",
                size_bytes=128,
                content_type="application/pdf",
                blob_ref={
                    "url": "https://attacker.example/uploads/demo.pdf",
                    "pathname": "uploads/demo.pdf",
                },
            )

    def test_delete_document_removes_upload_job_artifacts_and_job_directory(self):
        from services import document_service as svc

        deleted_blobs = []
        deleted_jobs = []

        class FakeAppRepo:
            def get_document(self, doc_id):
                return {"doc_id": doc_id, "blob_ref": {"url": "https://private.example/upload.pdf"}}

            def list_all_jobs(self):
                return [{
                    "job_id": "job_1",
                    "doc_id": "doc_1",
                    "artifacts": {
                        "stats.json": {"url": "https://private.example/stats.json"},
                        "ignored": "not-a-reference",
                    },
                }]

            def delete_job(self, job_id):
                deleted_jobs.append(job_id)

            def delete_document(self, _doc_id):
                return True

        class FakeBlobRepo:
            def delete(self, blob_ref):
                deleted_blobs.append(blob_ref)

        with (
            patch.object(svc.app_store, "get_app_repository", return_value=FakeAppRepo()),
            patch.object(svc.graph_store, "get_graph_repository", return_value=type("Graph", (), {
                "remove_document": lambda self, _doc_id: (2, 1),
            })()),
            patch.object(svc.blob_store, "get_blob_repository", return_value=FakeBlobRepo()),
            patch.object(svc.fs, "delete_job") as delete_job_dir,
        ):
            result = svc.delete_document("doc_1")

        self.assertEqual(result, (True, 2, 1))
        self.assertEqual(deleted_jobs, ["job_1"])
        self.assertEqual(deleted_blobs, [
            {"url": "https://private.example/upload.pdf"},
            {"url": "https://private.example/stats.json"},
        ])
        delete_job_dir.assert_called_once_with("job_1")


if __name__ == "__main__":
    unittest.main()
