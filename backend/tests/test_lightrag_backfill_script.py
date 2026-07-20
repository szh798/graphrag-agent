from __future__ import annotations

import sys
import unittest
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FakeClient:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def request(self, method, path, *, query=None, payload=None):
        self.calls.append({"method": method, "path": path, "query": query, "payload": payload})
        if method == "GET":
            return self.pages.pop(0)
        return {"code": 0, "data": {"job_id": f"job-{path.rsplit('/', 2)[-2]}"}}


class LightRAGBackfillScriptTests(unittest.TestCase):
    def test_dry_run_never_posts_and_skips_completed_or_active_indexes(self):
        from scripts import lightrag_backfill as backfill

        client = FakeClient([{
            "code": 0,
            "data": {
                "total": 4,
                "items": [
                    {"doc_id": "missing", "filename": "a.md", "status": "indexed", "indexes": {}},
                    {"doc_id": "done", "status": "indexed", "indexes": {"lightrag": {"status": "done"}}},
                    {"doc_id": "active", "status": "indexed", "indexes": {"lightrag": {"status": "indexing"}}},
                    {"doc_id": "legacy-pending", "status": "uploaded", "indexes": {}},
                ],
            },
        }])

        report = backfill.backfill_documents(client, apply=False, page_size=100)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["documents_planned"], 1)
        self.assertEqual(report["planned"][0]["doc_id"], "missing")
        self.assertEqual([call["method"] for call in client.calls], ["GET"])

    def test_apply_posts_only_selected_documents_to_configured_retry_path(self):
        from scripts import lightrag_backfill as backfill

        client = FakeClient([{
            "data": {
                "total": 2,
                "items": [
                    {"doc_id": "doc-1", "status": "indexed", "indexes": {}},
                    {"doc_id": "doc-2", "status": "indexed", "indexes": {"lightrag": {"status": "failed"}}},
                ],
            },
            "code": 0,
        }])

        report = backfill.backfill_documents(
            client,
            apply=True,
            include_failed=True,
            retry_path_template="/custom/{doc_id}/retry",
        )

        self.assertFalse(report["dry_run"])
        self.assertEqual(report["documents_enqueued"], 2)
        post_calls = [call for call in client.calls if call["method"] == "POST"]
        self.assertEqual([call["path"] for call in post_calls], ["/custom/doc-1/retry", "/custom/doc-2/retry"])
        self.assertTrue(all(call["payload"] == {"engine": "lightrag"} for call in post_calls))

    def test_document_listing_uses_limit_offset_pagination(self):
        from scripts import lightrag_backfill as backfill

        client = FakeClient([
            {"code": 0, "data": {"total": 3, "items": [{"doc_id": "1"}, {"doc_id": "2"}]}},
            {"code": 0, "data": {"total": 3, "items": [{"doc_id": "3"}]}},
        ])

        documents = backfill.list_all_documents(client, documents_path="/documents", page_size=2)

        self.assertEqual([item["doc_id"] for item in documents], ["1", "2", "3"])
        self.assertEqual(client.calls[0]["query"], {"limit": 2, "offset": 0})
        self.assertEqual(client.calls[1]["query"], {"limit": 2, "offset": 2})


if __name__ == "__main__":
    unittest.main()
