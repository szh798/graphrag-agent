"""Indexing queue repository backends."""
from __future__ import annotations

import json
import os
import time
from urllib.parse import quote

import requests

INDEX_QUEUE_KEY = os.getenv("INDEX_QUEUE_KEY", "graphrag:index:queue")
INDEX_PROCESSING_KEY = os.getenv("INDEX_PROCESSING_KEY", f"{INDEX_QUEUE_KEY}:processing")
INDEX_JOB_LEASE_SECONDS = max(60, int(os.getenv("INDEX_JOB_LEASE_SECONDS", "330")))
INDEX_JOB_MAX_RECOVERIES = max(0, int(os.getenv("INDEX_JOB_MAX_RECOVERIES", "2")))

_CLAIM_SCRIPT = """
local raw = redis.call('RPOP', KEYS[1])
if not raw then return nil end
redis.call('ZADD', KEYS[2], ARGV[1], raw)
return raw
""".strip()

_RECOVER_SCRIPT = """
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
local recovered = {}
local exhausted = {}
for _, raw in ipairs(expired) do
  if redis.call('ZREM', KEYS[2], raw) == 1 then
    local ok, payload = pcall(cjson.decode, raw)
    if ok and type(payload) == 'table' then
      local attempt = tonumber(payload.attempt or 0) + 1
      payload.attempt = attempt
      local job_id = tostring(payload.job_id or '')
      if attempt <= tonumber(ARGV[3]) then
        redis.call('LPUSH', KEYS[1], cjson.encode(payload))
        table.insert(recovered, job_id)
      else
        table.insert(exhausted, job_id)
      end
    else
      table.insert(exhausted, '')
    end
  end
end
return cjson.encode({recovered = recovered, exhausted = exhausted})
""".strip()


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

    def ack_index_job(self, payload: dict) -> None:
        return None

    def recover_expired_jobs(self) -> dict[str, list[str]]:
        return {"recovered": [], "exhausted": []}


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
            "lease_seconds": INDEX_JOB_LEASE_SECONDS,
            "max_recoveries": INDEX_JOB_MAX_RECOVERIES,
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
        queued = {**payload, "attempt": int(payload.get("attempt") or 0)}
        self._command("LPUSH", INDEX_QUEUE_KEY, json.dumps(queued, ensure_ascii=False, separators=(",", ":")))

    def pop_index_job(self, timeout_seconds: int = 5) -> dict | None:
        del timeout_seconds  # Claim is intentionally non-blocking for serverless dispatchers.
        lease_expires_at = int(time.time()) + INDEX_JOB_LEASE_SECONDS
        result = self._command(
            "EVAL",
            _CLAIM_SCRIPT,
            "2",
            INDEX_QUEUE_KEY,
            INDEX_PROCESSING_KEY,
            str(lease_expires_at),
        ).get("result")
        if not result:
            return None
        raw = result[1] if isinstance(result, list) and len(result) > 1 else result
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        payload["_queue_receipt"] = raw
        return payload

    def ack_index_job(self, payload: dict) -> None:
        receipt = payload.get("_queue_receipt")
        if receipt:
            self._command("ZREM", INDEX_PROCESSING_KEY, str(receipt))

    def recover_expired_jobs(self) -> dict[str, list[str]]:
        result = self._command(
            "EVAL",
            _RECOVER_SCRIPT,
            "2",
            INDEX_QUEUE_KEY,
            INDEX_PROCESSING_KEY,
            str(int(time.time())),
            "25",
            str(INDEX_JOB_MAX_RECOVERIES),
        ).get("result")
        if not result:
            return {"recovered": [], "exhausted": []}
        recovered = json.loads(result) if isinstance(result, str) else result
        return {
            "recovered": [str(job_id) for job_id in recovered.get("recovered", []) if job_id],
            "exhausted": [str(job_id) for job_id in recovered.get("exhausted", []) if job_id],
        }


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
