"""OpenAI-compatible embedding client."""
from __future__ import annotations

import requests

from pipeline.llm_config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_EMBEDDING_DIMENSIONS,
    LLM_EMBEDDING_MODEL,
    require_llm_api_key,
)


def _endpoint() -> str:
    return LLM_BASE_URL.rstrip("/") + "/embeddings"


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    require_llm_api_key()
    response = requests.post(
        _endpoint(),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_EMBEDDING_MODEL,
            "input": texts,
            "dimensions": LLM_EMBEDDING_DIMENSIONS,
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    return [item["embedding"] for item in payload.get("data", [])]


def embed_text(text: str) -> list[float] | None:
    vectors = embed_texts([text])
    return vectors[0] if vectors else None
