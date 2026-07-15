"""Indexing queue repository backends."""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import requests

INDEX_QUEUE_KEY = os.getenv("INDEX_QUEUE_KEY", "graphrag:index:queue")


class LocalThreadQueueRepository:
    def profile(self) -> dict:
        return {"backend": "local_thread", "durable": False}

    def health(self) -> dict:
        return {
            "status": "ok",
            "backend": "local_thread",
            "durable": False,
            "warning": "Local thread queue is for development only; production should use Upstash Redis or a worker queue.",
        }

    def is_durable(self) -> bool:
        return False

    def enqueue_index_job(self, payload: dict) -> None:
        return None

    def pop_index_job(self, timeout_seconds: int = 5) -> dict | None:
        return None


class UpstashRedisQueueRepository:
    def __init__(self):
        self.rest_url = (os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL", "")).strip().rstrip("/")
        self.rest_token = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN", "")).strip()

    def profile(self) -> dict:
        return {
            "backend": "upstash",
            "url_configured": bool(self.rest_url),
            "token_configured": bool(self.rest_token),
            "durable": True,
        }

    def health(self) -> dict:
        if not self.rest_url or not self.rest_token:
            return {"status": "error", **self.profile(), "error": "UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required"}
        try:
            response = requests.get(
                f"{self.rest_url}/ping",
                headers={"Authorization": f"Bearer {self.rest_token}"},
                timeout=5,
            )
            response.raise_for_status()
            return {"status": "ok", **self.profile()}
        except Exception as exc:
            return {"status": "error", **self.profile(), "error": str(exc)}

    def is_durable(self) -> bool:
        return True

    def _command(self, *parts: str) -> dict:
        if not self.rest_url or not self.rest_token:
            raise ValueError("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are required")
        response = requests.post(
            "/".join([self.rest_url, *[quote(str(part).strip("/"), safe="") for part in parts]]),
            headers={"Authorization": f"Bearer {self.rest_token}"},
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def enqueue_index_job(self, payload: dict) -> None:
        self._command("LPUSH", INDEX_QUEUE_KEY, json.dumps(payload, ensure_ascii=False))

    def pop_index_job(self, timeout_seconds: int = 5) -> dict | None:
        result = self._command("BRPOP", INDEX_QUEUE_KEY, str(max(1, timeout_seconds))).get("result")
        if not result:
            return None
        raw = result[1] if isinstance(result, list) and len(result) > 1 else result
        return json.loads(raw) if isinstance(raw, str) else raw


_CACHE_KEY: tuple[str, str, str] | None = None
_CACHE_REPO: LocalThreadQueueRepository | UpstashRedisQueueRepository | None = None


def get_queue_repository() -> LocalThreadQueueRepository | UpstashRedisQueueRepository:
    global _CACHE_KEY, _CACHE_REPO
    backend = os.getenv("GRAPHRAG_QUEUE_BACKEND", "local_thread").strip().lower()
    if backend in {"thread", "threads", "local", "memory", "in_memory"}:
        backend = "local_thread"
    key = (
        backend,
        os.getenv("UPSTASH_REDIS_REST_URL") or os.getenv("KV_REST_API_URL", ""),
        os.getenv("UPSTASH_REDIS_REST_TOKEN") or os.getenv("KV_REST_API_TOKEN", ""),
    )
    if _CACHE_REPO is not None and _CACHE_KEY == key:
        return _CACHE_REPO
    _CACHE_KEY = key
    _CACHE_REPO = UpstashRedisQueueRepository() if backend in {"upstash", "redis", "upstash_redis"} else LocalThreadQueueRepository()
    return _CACHE_REPO


def reset_queue_repository_cache() -> None:
    global _CACHE_KEY, _CACHE_REPO
    _CACHE_KEY = None
    _CACHE_REPO = None
