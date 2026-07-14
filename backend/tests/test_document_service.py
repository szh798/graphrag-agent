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

    def test_upload_content_checks_mime_and_magic(self):
        from services.document_service import validate_upload_content

        valid_pdf = validate_upload_content("demo.pdf", "application/pdf", b"%PDF-1.7\n", 9)
        wrong_magic = validate_upload_content("demo.pdf", "application/pdf", b"not a pdf", 9)
        wrong_mime = validate_upload_content("demo.png", "text/plain", b"\x89PNG\r\n\x1a\n", 8)

        self.assertTrue(valid_pdf[0])
        self.assertEqual(wrong_magic[1], 1002)
        self.assertEqual(wrong_mime[1], 1002)

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


if __name__ == "__main__":
    unittest.main()
