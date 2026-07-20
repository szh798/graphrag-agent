"""Explicit model bindings for the embedded LightRAG Core runtime.

LightRAG Core does not consume the API server's binding environment by
itself.  The product runtime therefore builds and injects concrete LLM,
embedding, and reranker callables.  Imports from ``lightrag`` intentionally
remain inside :func:`build_provider_bindings`, so the public gateway can load
the facade without installing the optional engine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .errors import LightRAGConfigurationError


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _required_env(label: str, *names: str) -> str:
    value = _first_env(*names)
    if not value:
        joined = ", ".join(names)
        raise LightRAGConfigurationError(
            f"{label} is required for the LightRAG runtime ({joined})"
        )
    return value


def _positive_int(label: str, raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise LightRAGConfigurationError(f"{label} must be a positive integer") from exc
    if value <= 0:
        raise LightRAGConfigurationError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ProviderSettings:
    llm_api_key: str = field(repr=False)
    llm_base_url: str
    extract_model: str
    keyword_model: str
    query_model: str
    llm_temperature: float
    embedding_api_key: str = field(repr=False)
    embedding_base_url: str
    embedding_model: str
    embedding_dim: int
    embedding_max_tokens: int
    rerank_api_key: str = field(repr=False)
    rerank_base_url: str
    rerank_model: str


@dataclass(frozen=True)
class ProviderBindings:
    settings: ProviderSettings
    llm_model_func: Any
    embedding_func: Any
    rerank_model_func: Any
    role_llm_configs: dict[str, dict[str, Any]]


def load_provider_settings() -> ProviderSettings:
    """Load and validate the complete production provider contract.

    Configuration is fail-closed.  A partially configured LightRAG worker must
    fail readiness/initialization rather than silently use OpenAI defaults or
    run without the required multilingual reranker.
    """

    llm_api_key = _required_env(
        "OpenAI-compatible LLM API key",
        "LIGHTRAG_LLM_API_KEY",
        "LLM_BINDING_API_KEY",
        "LLM_API_KEY",
    )
    llm_base_url = _required_env(
        "OpenAI-compatible LLM base URL",
        "LIGHTRAG_LLM_BASE_URL",
        "LLM_BINDING_HOST",
        "LLM_BASE_URL",
    )
    extract_model = _required_env(
        "LightRAG extraction model",
        "LIGHTRAG_INDEX_MODEL",
        "EXTRACT_LLM_MODEL",
        "LLM_INDEX_MODEL",
        "LLM_MODEL",
    )
    keyword_model = _first_env(
        "LIGHTRAG_KEYWORD_MODEL",
        "KEYWORD_LLM_MODEL",
        default=extract_model,
    )
    query_model = _first_env(
        "LIGHTRAG_QUERY_MODEL",
        "QUERY_LLM_MODEL",
        "LLM_MODEL",
        default=extract_model,
    )
    try:
        llm_temperature = float(
            _first_env("LIGHTRAG_LLM_TEMPERATURE", "LLM_TEMPERATURE", default="0.1")
        )
    except ValueError as exc:
        raise LightRAGConfigurationError(
            "LIGHTRAG_LLM_TEMPERATURE must be numeric"
        ) from exc

    embedding_api_key = _first_env(
        "LIGHTRAG_EMBEDDING_API_KEY",
        "EMBEDDING_BINDING_API_KEY",
        default=llm_api_key,
    )
    embedding_base_url = _first_env(
        "LIGHTRAG_EMBEDDING_BASE_URL",
        "EMBEDDING_BINDING_HOST",
        default=llm_base_url,
    )
    embedding_model = _required_env(
        "LightRAG embedding model",
        "LIGHTRAG_EMBEDDING_MODEL",
        "EMBEDDING_MODEL",
        "LLM_EMBEDDING_MODEL",
    )
    embedding_dim = _positive_int(
        "LightRAG embedding dimension",
        _required_env(
            "LightRAG embedding dimension",
            "LIGHTRAG_EMBEDDING_DIM",
            "EMBEDDING_DIM",
            "LLM_EMBEDDING_DIMENSIONS",
        ),
    )
    embedding_max_tokens = _positive_int(
        "LightRAG embedding max tokens",
        _first_env(
            "LIGHTRAG_EMBEDDING_MAX_TOKENS",
            "EMBEDDING_MAX_TOKEN_SIZE",
            default="8192",
        ),
    )

    rerank_api_key = _required_env(
        "LightRAG reranker API key",
        "LIGHTRAG_RERANK_API_KEY",
        "RERANK_BINDING_API_KEY",
    )
    rerank_base_url = _required_env(
        "LightRAG reranker base URL",
        "LIGHTRAG_RERANK_BASE_URL",
        "RERANK_BINDING_HOST",
    )
    rerank_model = _first_env(
        "LIGHTRAG_RERANK_MODEL",
        "RERANK_MODEL",
        default="BAAI/bge-reranker-v2-m3",
    )

    return ProviderSettings(
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        extract_model=extract_model,
        keyword_model=keyword_model,
        query_model=query_model,
        llm_temperature=llm_temperature,
        embedding_api_key=embedding_api_key,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        embedding_max_tokens=embedding_max_tokens,
        rerank_api_key=rerank_api_key,
        rerank_base_url=rerank_base_url,
        rerank_model=rerank_model,
    )


def build_provider_bindings(
    settings: ProviderSettings | None = None,
) -> ProviderBindings:
    """Build concrete LightRAG 1.5.4 Core provider functions."""

    settings = settings or load_provider_settings()
    try:
        from lightrag.llm.openai import (  # type: ignore[import-not-found]
            openai_complete_if_cache,
            openai_embed,
        )
        from lightrag.rerank import generic_rerank_api  # type: ignore[import-not-found]
        from lightrag.utils import (  # type: ignore[import-not-found]
            wrap_embedding_func_with_attrs,
        )
    except (ImportError, ModuleNotFoundError) as exc:
        raise LightRAGConfigurationError(
            "LightRAG provider modules are unavailable; install lightrag-hku==1.5.4"
        ) from exc

    def completion_for(model: str):
        async def complete(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> Any:
            # The role wrapper may provide generic model/binding kwargs.  This
            # integration owns those values and removes duplicates so they are
            # never forwarded as unsupported OpenAI request parameters.
            for key in (
                "model",
                "model_name",
                "llm_model_name",
                "base_url",
                "api_key",
            ):
                kwargs.pop(key, None)
            kwargs.setdefault("temperature", settings.llm_temperature)
            return await openai_complete_if_cache(
                model,
                prompt,
                system_prompt=system_prompt,
                history_messages=history_messages or [],
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                **kwargs,
            )

        return complete

    extract_func = completion_for(settings.extract_model)
    keyword_func = completion_for(settings.keyword_model)
    query_func = completion_for(settings.query_model)

    @wrap_embedding_func_with_attrs(
        embedding_dim=settings.embedding_dim,
        max_token_size=settings.embedding_max_tokens,
        model_name=settings.embedding_model,
        supports_asymmetric=True,
    )
    async def embedding_func(
        texts: list[str],
        context: str = "document",
        embedding_dim: int | None = None,
        max_token_size: int | None = None,
        **kwargs: Any,
    ) -> Any:
        # Calling .func is required by LightRAG: openai_embed is itself an
        # EmbeddingFunc, and invoking it directly would double-inject attrs.
        kwargs.pop("model", None)
        kwargs.pop("base_url", None)
        kwargs.pop("api_key", None)
        provider_kwargs = {
            key: kwargs[key]
            for key in (
                "client_configs",
                "token_tracker",
                "query_prefix",
                "document_prefix",
            )
            if key in kwargs
        }
        return await openai_embed.func(
            texts,
            model=settings.embedding_model,
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key,
            embedding_dim=embedding_dim or settings.embedding_dim,
            max_token_size=max_token_size or settings.embedding_max_tokens,
            context=context,
            **provider_kwargs,
        )

    async def rerank_model_func(
        query: str,
        documents: list[str],
        top_n: int | None = None,
        **kwargs: Any,
    ) -> Any:
        # LightRAG can add internal queue kwargs; only provider-supported
        # extension payloads are forwarded deliberately.
        extra_body = kwargs.pop("extra_body", None)
        return await generic_rerank_api(
            query=query,
            documents=documents,
            model=settings.rerank_model,
            base_url=settings.rerank_base_url,
            api_key=settings.rerank_api_key,
            top_n=top_n,
            return_documents=False,
            extra_body=extra_body,
            response_format="standard",
            request_format="standard",
        )

    def role(func: Any) -> dict[str, Any]:
        # Keep the plain-dict contract to the four stable RoleLLMConfig fields
        # accepted by the pinned Core version. Model identity is bound inside
        # each closure rather than passed as an extra configuration key.
        return {
            "func": func,
            "kwargs": {},
            "max_async": None,
            "timeout": None,
        }

    # VLM is intentionally mapped but disabled by the Core constructor.  The
    # existing parser produces page text once, so no second multimodal parse is
    # performed inside LightRAG.
    role_llm_configs = {
        "extract": role(extract_func),
        "keyword": role(keyword_func),
        "query": role(query_func),
        "vlm": role(extract_func),
    }
    return ProviderBindings(
        settings=settings,
        llm_model_func=extract_func,
        embedding_func=embedding_func,
        rerank_model_func=rerank_model_func,
        role_llm_configs=role_llm_configs,
    )


__all__ = [
    "ProviderBindings",
    "ProviderSettings",
    "build_provider_bindings",
    "load_provider_settings",
]
