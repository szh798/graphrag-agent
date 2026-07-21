from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class VercelBlobRepositoryTests(unittest.TestCase):
    def test_private_store_is_used_for_put_get_and_delete(self):
        from storage.blob_repository import VercelBlobRepository

        @dataclass
        class PutResult:
            url: str
            download_url: str
            pathname: str
            content_type: str
            content_disposition: str

        calls: list[tuple[str, tuple, dict]] = []
        client = SimpleNamespace(
            put=lambda *args, **kwargs: calls.append(("put", args, kwargs)) or PutResult(
                url="https://private.example/jobs/job_1/stats.json",
                download_url="https://private.example/jobs/job_1/stats.json?download=1",
                pathname="jobs/job_1/stats.json",
                content_type="application/json",
                content_disposition="attachment",
            ),
            get=lambda *args, **kwargs: calls.append(("get", args, kwargs)) or SimpleNamespace(content=b"{}"),
            delete=lambda *args, **kwargs: calls.append(("delete", args, kwargs)),
        )

        with patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "token", "BLOB_ACCESS": "private"}, clear=False):
            repo = VercelBlobRepository()
            with patch.object(repo, "_client", return_value=client):
                saved = repo.save_json("jobs/job_1/stats.json", {"ok": True})
                loaded = repo.read_bytes(saved)
                repo.delete(saved)

        self.assertEqual(saved["pathname"], "jobs/job_1/stats.json")
        self.assertEqual(loaded, b"{}")
        self.assertEqual([call[0] for call in calls], ["put", "get", "delete"])
        self.assertEqual(calls[0][2]["access"], "private")
        self.assertTrue(calls[0][2]["overwrite"])
        self.assertEqual(calls[0][2]["token"], "token")
        self.assertEqual(calls[1][2]["access"], "private")
        self.assertEqual(calls[2][2]["token"], "token")

    def test_repeated_artifact_write_is_idempotent_for_queue_recovery(self):
        from storage.blob_repository import VercelBlobRepository

        stored: dict[str, bytes] = {}
        calls: list[dict] = []

        def put(path, body, **kwargs):
            calls.append(kwargs)
            if path in stored and not kwargs.get("overwrite"):
                raise RuntimeError("Vercel Blob returned HTTP 400: pathname already exists")
            stored[path] = body
            return {
                "url": f"https://private.example/{path}",
                "download_url": f"https://private.example/{path}?download=1",
                "pathname": path,
            }

        client = SimpleNamespace(put=put)
        key = "jobs/job_recovered/parsed_pages.json"

        with patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "token", "BLOB_ACCESS": "private"}, clear=False):
            repo = VercelBlobRepository()
            with patch.object(repo, "_client", return_value=client):
                first = repo.save_json(key, [{"page": 1, "text": "first"}])
                second = repo.save_json(key, [{"page": 1, "text": "recovered"}])

        self.assertEqual(first["pathname"], key)
        self.assertEqual(second["pathname"], key)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["overwrite"] for call in calls))
        self.assertIn(b"recovered", stored[key])

    def test_source_upload_does_not_replace_an_existing_blob(self):
        from storage.blob_repository import VercelBlobRepository

        calls: list[dict] = []
        client = SimpleNamespace(
            put=lambda *args, **kwargs: calls.append(kwargs) or {
                "url": "https://private.example/doc.pdf",
                "download_url": "https://private.example/doc.pdf?download=1",
                "pathname": "doc.pdf",
            }
        )

        with patch.dict("os.environ", {"BLOB_READ_WRITE_TOKEN": "token"}, clear=False):
            repo = VercelBlobRepository()
            with patch.object(repo, "_client", return_value=client):
                repo.save_upload("doc.pdf", b"pdf")

        self.assertFalse(calls[0]["overwrite"])


if __name__ == "__main__":
    unittest.main()
