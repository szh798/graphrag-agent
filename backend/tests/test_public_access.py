from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class PublicAccessTests(unittest.TestCase):
    def test_public_proxy_request_uses_allowlist(self):
        from public_access import public_document_ids

        with patch.dict("os.environ", {"PUBLIC_DOCUMENT_IDS": "doc_a, doc_b"}):
            self.assertEqual(public_document_ids("1"), {"doc_a", "doc_b"})

    def test_public_proxy_request_fails_closed_without_allowlist(self):
        from public_access import public_document_ids

        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(public_document_ids("1"), set())

    def test_trusted_management_request_keeps_full_access(self):
        from public_access import public_document_ids

        with patch.dict("os.environ", {"PUBLIC_DOCUMENT_IDS": "doc_public"}):
            self.assertIsNone(public_document_ids(None))
            self.assertIsNone(public_document_ids("0"))

    def test_document_visibility_is_explicit(self):
        from public_access import document_is_visible

        self.assertTrue(document_is_visible("doc_any", None))
        self.assertTrue(document_is_visible("doc_public", {"doc_public"}))
        self.assertFalse(document_is_visible("doc_private", {"doc_public"}))


if __name__ == "__main__":
    unittest.main()
