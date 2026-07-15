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
        self.assertEqual(calls[0][2]["token"], "token")
        self.assertEqual(calls[1][2]["access"], "private")
        self.assertEqual(calls[2][2]["token"], "token")


if __name__ == "__main__":
    unittest.main()
