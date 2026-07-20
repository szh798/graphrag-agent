from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from lightrag_integration.errors import LightRAGConfigurationError  # noqa: E402
from lightrag_integration.providers import (  # noqa: E402
    ProviderSettings,
    build_provider_bindings,
    load_provider_settings,
)


class FakeEmbeddingFunc:
    def __init__(self, func, attrs):
        self.func = func
        for key, value in attrs.items():
            setattr(self, key, value)


class LightRAGProviderTests(unittest.TestCase):
    def _modules(self, calls):
        root = types.ModuleType("lightrag")
        llm = types.ModuleType("lightrag.llm")
        openai = types.ModuleType("lightrag.llm.openai")
        rerank = types.ModuleType("lightrag.rerank")
        utils = types.ModuleType("lightrag.utils")

        async def complete(model, prompt, **kwargs):
            calls["complete"] = (model, prompt, kwargs)
            return "ok"

        async def embed(texts, **kwargs):
            calls["embed"] = (texts, kwargs)
            return [[0.1, 0.2]]

        async def rerank_call(**kwargs):
            calls["rerank"] = kwargs
            return [{"index": 0, "relevance_score": 1.0}]

        def decorator(**attrs):
            return lambda func: FakeEmbeddingFunc(func, attrs)

        openai.openai_complete_if_cache = complete
        openai.openai_embed = types.SimpleNamespace(func=embed)
        rerank.generic_rerank_api = rerank_call
        utils.wrap_embedding_func_with_attrs = decorator
        return {
            "lightrag": root,
            "lightrag.llm": llm,
            "lightrag.llm.openai": openai,
            "lightrag.rerank": rerank,
            "lightrag.utils": utils,
        }

    @staticmethod
    def _settings():
        return ProviderSettings(
            llm_api_key="llm-key",
            llm_base_url="https://glm.example/v1",
            extract_model="glm-extract",
            keyword_model="glm-keyword",
            query_model="glm-query",
            llm_temperature=0.1,
            embedding_api_key="embed-key",
            embedding_base_url="https://embed.example/v1",
            embedding_model="embedding-3",
            embedding_dim=1024,
            embedding_max_tokens=8192,
            rerank_api_key="rerank-key",
            rerank_base_url="https://rerank.example/v1/rerank",
            rerank_model="BAAI/bge-reranker-v2-m3",
        )

    def test_builds_all_explicit_core_functions_and_role_models(self):
        calls = {}
        with patch.dict(sys.modules, self._modules(calls)):
            bindings = build_provider_bindings(self._settings())
            self.assertEqual(
                set(bindings.role_llm_configs),
                {"extract", "keyword", "query", "vlm"},
            )
            self.assertTrue(all(
                set(config) == {"func", "kwargs", "max_async", "timeout"}
                for config in bindings.role_llm_configs.values()
            ))
            self.assertEqual(bindings.embedding_func.embedding_dim, 1024)
            self.assertEqual(bindings.embedding_func.model_name, "embedding-3")

            result = asyncio.run(bindings.role_llm_configs["query"]["func"](
                "question",
                model="untrusted-override",
                base_url="https://untrusted.example",
            ))
            self.assertEqual(result, "ok")
            model, _, kwargs = calls["complete"]
            self.assertEqual(model, "glm-query")
            self.assertEqual(kwargs["base_url"], "https://glm.example/v1")
            self.assertEqual(kwargs["api_key"], "llm-key")
            self.assertNotIn("model", kwargs)

            asyncio.run(bindings.embedding_func.func(["page"], context="document"))
            _, embed_kwargs = calls["embed"]
            self.assertEqual(embed_kwargs["model"], "embedding-3")
            self.assertEqual(embed_kwargs["base_url"], "https://embed.example/v1")
            self.assertEqual(embed_kwargs["embedding_dim"], 1024)
            self.assertEqual(embed_kwargs["max_token_size"], 8192)

            asyncio.run(bindings.rerank_model_func(
                "question", ["first", "second"], top_n=1
            ))
            self.assertEqual(calls["rerank"]["model"], "BAAI/bge-reranker-v2-m3")
            self.assertEqual(calls["rerank"]["response_format"], "standard")
            self.assertEqual(calls["rerank"]["request_format"], "standard")

    def test_missing_reranker_configuration_fails_closed(self):
        env = {
            "LLM_API_KEY": "key",
            "LLM_BASE_URL": "https://glm.example/v1",
            "LLM_INDEX_MODEL": "glm-index",
            "LLM_MODEL": "glm-query",
            "LLM_EMBEDDING_MODEL": "embedding-3",
            "LLM_EMBEDDING_DIMENSIONS": "1024",
            "LIGHTRAG_RERANK_API_KEY": "",
            "RERANK_BINDING_API_KEY": "",
            "LIGHTRAG_RERANK_BASE_URL": "",
            "RERANK_BINDING_HOST": "",
        }
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(LightRAGConfigurationError):
                load_provider_settings()


if __name__ == "__main__":
    unittest.main()
