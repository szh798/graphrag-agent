from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class EmbeddingTests(unittest.TestCase):
    def test_embed_texts_calls_openai_compatible_endpoint_with_dimensions(self):
        import pipeline.embeddings as embeddings

        calls = []

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

        def fake_post(url, headers, json, timeout):
            calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
            return Response()

        with (
            patch.object(embeddings.requests, "post", side_effect=fake_post),
            patch.object(embeddings, "LLM_API_KEY", "secret"),
            patch.object(embeddings, "LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/"),
            patch.object(embeddings, "LLM_EMBEDDING_MODEL", "embedding-3"),
            patch.object(embeddings, "LLM_EMBEDDING_DIMENSIONS", 1024),
        ):
            vectors = embeddings.embed_texts(["hello"])

        self.assertEqual(vectors, [[0.1, 0.2, 0.3]])
        self.assertEqual(calls[0]["url"], "https://open.bigmodel.cn/api/paas/v4/embeddings")
        self.assertEqual(calls[0]["json"]["model"], "embedding-3")
        self.assertEqual(calls[0]["json"]["dimensions"], 1024)


if __name__ == "__main__":
    unittest.main()
