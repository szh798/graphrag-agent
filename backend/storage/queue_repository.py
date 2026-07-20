"""Indexing queue repository backends."""
from __future__ import annotations

import json
import hashlib
import os
import threading
import time
from urllib.parse import quote

import requests

INDEX_QUEUE_KEY = os.getenv("INDEX_QUEUE_KEY", "graphrag:index:queue")
INDEX_PROCESSING_KEY = os.getenv("INDEX_PROCESSING_KEY", f"{INDEX_QUEUE_KEY}:processing")
INDEX_JOB_LEASE_SECONDS = max(60, int(os.getenv("INDEX_JOB_LEASE_SECONDS", "330")))
INDEX_JOB_MAX_RECOVERIES = max(0, int(os.getenv("INDEX_JOB_MAX_RECOVERIES", "2")))
INDEX_OWNER_LOCK_SECONDS = max(
    INDEX_JOB_LEASE_SECONDS + 60,
    int(os.getenv("INDEX_OWNER_LOCK_SECONDS", str(INDEX_JOB_LEASE_SECONDS * 2))),
)
INDEX_OWNER_LOCK_PREFIX = os.getenv(
    "INDEX_OWNER_LOCK_PREFIX", "graphrag:index:owner-lock"
)

_LOCAL_OWNER_LOCKS: dict[str, str] = {}
_LOCAL_OWNER_LOCKS_GUARD = threading.Lock()
_LOCAL_WORKER_HEARTBEAT: dict | None = None
_LOCAL_WORKER_HEARTBEAT_GUARD = threading.Lock()

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

_RELEASE_OWNER_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""".strip()

_REFRESH_OWNER_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
""".strip()

_REFRESH_PROCESSING_LEASE_SCRIPT = """
if redis.call('ZSCORE', KEYS[1], ARGV[1]) then
  redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
  return 1
end
return 0
""".strip()


def _owner_lock_key(owner_id: str) -> str:
    # Tenant identifiers can contain Clerk user/org IDs. Redis keys expose only
    # a deterministic digest, never the raw identity.
    digest = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()
    return f"{INDEX_OWNER_LOCK_PREFIX}:{digest}"


def worker_heartbeat_key() -> str:
    """Return the shared worker heartbeat key without caching env configuration."""
    return os.getenv(
        "INDEX_WORKER_HEARTBEAT_KEY",
        f"{INDEX_QUEUE_KEY}:worker-heartbeat",
    ).strip() or f"{INDEX_QUEUE_KEY}:worker-heartbeat"


def worker_heartbeat_ttl_seconds() -> int:
    """Bound heartbeat expiry so a bad env value cannot create immortal health."""
    try:
        configured = int(os.getenv("INDEX_WORKER_HEARTBEAT_TTL_SECONDS", "120"))
    except (TypeError, ValueError):
        configured = 120
    return min(3600, max(30, configured))


def _heartbeat_payload(
    worker_id: str,
    version: str,
    *,
    last_seen: int | float | None = None,
) -> dict:
    normalized_worker_id = str(worker_id or "").strip()
    normalized_version = str(version or "").strip()
    if not normalized_worker_id:
        raise ValueError("worker_id is required")
    if not normalized_version:
        raise ValueError("worker version is required")
    observed_at = int(time.time() if last_seen is None else last_seen)
    if observed_at <= 0:
        raise ValueError("last_seen must be a positive Unix timestamp")
    return {
        "worker_id": normalized_worker_id,
        "version": normalized_version,
        "last_seen": observed_at,
    }


def _heartbeat_status(payload: dict | None) -> dict | None:
    if not isinstance(payload, dict):
        return None
    try:
        normalized = _heartbeat_payload(
            payload.get("worker_id", ""),
            payload.get("version", ""),
            last_seen=payload.get("last_seen"),
        )
    except (TypeError, ValueError):
        return {
            "fresh": False,
            "malformed": True,
            "ttl_seconds": worker_heartbeat_ttl_seconds(),
        }

    now = int(time.time())
    ttl_seconds = worker_heartbeat_ttl_seconds()
    # A heartbeat far in the future usually means the producer clock is bad;
    # accepting it could keep readiness green indefinitely.
    clock_skew_seconds = normalized["last_seen"] - now
    age_seconds = max(0, now - normalized["last_seen"])
    normalized.update({
        "fresh": age_seconds <= ttl_seconds and clock_skew_seconds <= 30,
        "age_seconds": age_seconds,
        "ttl_seconds": ttl_seconds,
    })
    return normalized


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

    def acquire_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        key = _owner_lock_key(owner_id)
        with _LOCAL_OWNER_LOCKS_GUARD:
            current = _LOCAL_OWNER_LOCKS.get(key)
            if current not in {None, job_id}:
                return False
            _LOCAL_OWNER_LOCKS[key] = job_id
            return True

    def refresh_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        key = _owner_lock_key(owner_id)
        with _LOCAL_OWNER_LOCKS_GUARD:
            return _LOCAL_OWNER_LOCKS.get(key) == job_id

    def release_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        key = _owner_lock_key(owner_id)
        with _LOCAL_OWNER_LOCKS_GUARD:
            if _LOCAL_OWNER_LOCKS.get(key) != job_id:
                return False
            _LOCAL_OWNER_LOCKS.pop(key, None)
            return True

    def refresh_index_job_lease(self, payload: dict) -> bool:
        del payload
        return True

    def record_worker_heartbeat(
        self,
        worker_id: str,
        version: str,
        *,
        last_seen: int | float | None = None,
    ) -> dict:
        global _LOCAL_WORKER_HEARTBEAT
        payload = _heartbeat_payload(worker_id, version, last_seen=last_seen)
        with _LOCAL_WORKER_HEARTBEAT_GUARD:
            _LOCAL_WORKER_HEARTBEAT = dict(payload)
        return _heartbeat_status(payload) or {}

    def get_worker_heartbeat(self) -> dict | None:
        with _LOCAL_WORKER_HEARTBEAT_GUARD:
            payload = dict(_LOCAL_WORKER_HEARTBEAT) if _LOCAL_WORKER_HEARTBEAT else None
        return _heartbeat_status(payload)


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

    def acquire_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        result = self._command(
            "SET",
            _owner_lock_key(owner_id),
            job_id,
            "NX",
            "EX",
            str(INDEX_OWNER_LOCK_SECONDS),
        ).get("result")
        return str(result or "").upper() == "OK"

    def refresh_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        result = self._command(
            "EVAL",
            _REFRESH_OWNER_LOCK_SCRIPT,
            "1",
            _owner_lock_key(owner_id),
            job_id,
            str(INDEX_OWNER_LOCK_SECONDS),
        ).get("result")
        return int(result or 0) == 1

    def release_index_owner_lock(self, owner_id: str, job_id: str) -> bool:
        result = self._command(
            "EVAL",
            _RELEASE_OWNER_LOCK_SCRIPT,
            "1",
            _owner_lock_key(owner_id),
            job_id,
        ).get("result")
        return int(result or 0) == 1

    def refresh_index_job_lease(self, payload: dict) -> bool:
        receipt = payload.get("_queue_receipt")
        if not receipt:
            return False
        lease_expires_at = int(time.time()) + INDEX_JOB_LEASE_SECONDS
        result = self._command(
            "EVAL",
            _REFRESH_PROCESSING_LEASE_SCRIPT,
            "1",
            INDEX_PROCESSING_KEY,
            str(receipt),
            str(lease_expires_at),
        ).get("result")
        return int(result or 0) == 1

    def record_worker_heartbeat(
        self,
        worker_id: str,
        version: str,
        *,
        last_seen: int | float | None = None,
    ) -> dict:
        payload = _heartbeat_payload(worker_id, version, last_seen=last_seen)
        result = self._command(
            "SET",
            worker_heartbeat_key(),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "EX",
            str(worker_heartbeat_ttl_seconds()),
        ).get("result")
        if str(result or "").upper() != "OK":
            raise RuntimeError("worker heartbeat was not persisted")
        return _heartbeat_status(payload) or {}

    def get_worker_heartbeat(self) -> dict | None:
        raw = self._command("GET", worker_heartbeat_key()).get("result")
        if raw is None:
            return None
        if isinstance(raw, dict):
            payload = raw
        else:
            try:
                payload = json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                return {
                    "fresh": False,
                    "malformed": True,
                    "ttl_seconds": worker_heartbeat_ttl_seconds(),
                }
        return _heartbeat_status(payload)


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
    global _CACHE_KEY, _CACHE_REPO, _LOCAL_WORKER_HEARTBEAT
    _CACHE_KEY = None
    _CACHE_REPO = None
    with _LOCAL_WORKER_HEARTBEAT_GUARD:
        _LOCAL_WORKER_HEARTBEAT = None
