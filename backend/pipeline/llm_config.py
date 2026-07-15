"""Shared OpenAI-compatible LLM configuration."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env", override=False)


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


LLM_PROVIDER = _first_env("LLM_PROVIDER", "ZAI_PROVIDER", default="deepseek")
if LLM_PROVIDER == "offline-demo":
    LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
else:
    LLM_API_KEY = _first_env("LLM_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY", "DEEPSEEK_API_KEY")
LLM_BASE_URL = _first_env(
    "LLM_BASE_URL",
    "ZAI_BASE_URL",
    "ZHIPU_BASE_URL",
    "DEEPSEEK_BASE_URL",
    default="https://api.deepseek.com",
)
LLM_MODEL = _first_env("LLM_MODEL", "ZAI_MODEL", "ZHIPU_MODEL", "DEEPSEEK_MODEL", default="deepseek-chat")
LLM_INDEX_MODEL = _first_env(
    "LLM_INDEX_MODEL",
    "LLM_EXTRACTION_MODEL",
    "ZAI_INDEX_MODEL",
    "ZAI_EXTRACTION_MODEL",
    "ZHIPU_INDEX_MODEL",
    "ZHIPU_EXTRACTION_MODEL",
    default=LLM_MODEL,
)
LLM_EMBEDDING_MODEL = _first_env(
    "LLM_EMBEDDING_MODEL",
    "ZAI_EMBEDDING_MODEL",
    "ZHIPU_EMBEDDING_MODEL",
    default="embedding-3",
)
LLM_EMBEDDING_DIMENSIONS = int(_first_env(
    "LLM_EMBEDDING_DIMENSIONS",
    "NEO4J_VECTOR_DIMENSIONS",
    default="1024",
))
LLM_TEMPERATURE = float(_first_env("LLM_TEMPERATURE", default="0"))


def require_llm_api_key() -> None:
    if not LLM_API_KEY:
        raise ValueError("LLM_API_KEY not set in backend/.env")
