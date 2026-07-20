"""Tenant workspace derivation and signed internal HTTP requests."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections.abc import Mapping
from urllib.parse import quote

import requests

from .errors import (
    LightRAGAuthenticationError,
    LightRAGConfigurationError,
    LightRAGValidationError,
)


WORKSPACE_PATTERN = re.compile(r"^ws_[0-9a-f]{40}$")
TIMESTAMP_HEADER = "X-LightRAG-Timestamp"
NONCE_HEADER = "X-LightRAG-Nonce"
CONTENT_SHA_HEADER = "X-LightRAG-Content-SHA256"
SIGNATURE_HEADER = "X-LightRAG-Signature"
SIGNATURE_VERSION = "v1"
_MIN_SECRET_BYTES = 32


def _secret(value: str | None, env_name: str) -> bytes:
    raw = value if value is not None else os.getenv(env_name, "")
    encoded = raw.strip().encode("utf-8")
    if len(encoded) < _MIN_SECRET_BYTES:
        raise LightRAGConfigurationError(f"{env_name} must contain at least {_MIN_SECRET_BYTES} bytes")
    return encoded


def workspace_key(tenant_id: str, *, secret: str | None = None) -> str:
    """Derive an opaque workspace; the raw tenant id is never embedded."""

    tenant = str(tenant_id or "").strip()
    if not tenant or len(tenant) > 512:
        raise LightRAGValidationError("a valid tenant id is required")
    digest = hmac.new(_secret(secret, "LIGHTRAG_WORKSPACE_SECRET"), tenant.encode("utf-8"), hashlib.sha256)
    return f"ws_{digest.hexdigest()[:40]}"


def validate_workspace(value: str) -> str:
    workspace = str(value or "").strip()
    if not WORKSPACE_PATTERN.fullmatch(workspace):
        raise LightRAGValidationError("invalid opaque workspace key")
    return workspace


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _canonical(method: str, path: str, timestamp: str, nonce: str, content_sha: str) -> bytes:
    normalized_path = "/" + str(path or "").lstrip("/")
    return "\n".join((method.upper(), normalized_path, timestamp, nonce, content_sha)).encode("utf-8")


def sign_request(
    method: str,
    path: str,
    body: bytes = b"",
    *,
    secret: str | None = None,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp_value = str(int(time.time() if timestamp is None else timestamp))
    nonce_value = str(nonce or secrets.token_urlsafe(18))
    if not 12 <= len(nonce_value) <= 128:
        raise LightRAGValidationError("invalid signing nonce")
    content_sha = body_sha256(body)
    signature = hmac.new(
        _secret(secret, "LIGHTRAG_HMAC_SECRET"),
        _canonical(method, path, timestamp_value, nonce_value, content_sha),
        hashlib.sha256,
    ).hexdigest()
    return {
        TIMESTAMP_HEADER: timestamp_value,
        NONCE_HEADER: nonce_value,
        CONTENT_SHA_HEADER: content_sha,
        SIGNATURE_HEADER: f"{SIGNATURE_VERSION}={signature}",
    }


class NonceReplayCache:
    """Replay fence with an optional fail-closed Upstash shared backend."""

    def __init__(self) -> None:
        self._items: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _distributed_settings() -> tuple[str, str, bool]:
        url = (
            os.getenv("LIGHTRAG_NONCE_REDIS_REST_URL")
            or os.getenv("UPSTASH_REDIS_REST_URL")
            or ""
        ).strip().rstrip("/")
        token = (
            os.getenv("LIGHTRAG_NONCE_REDIS_REST_TOKEN")
            or os.getenv("UPSTASH_REDIS_REST_TOKEN")
            or ""
        ).strip()
        required = os.getenv(
            "LIGHTRAG_REQUIRE_DISTRIBUTED_NONCE", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}
        return url, token, required

    @classmethod
    def distributed_profile(cls) -> dict[str, bool]:
        url, token, required = cls._distributed_settings()
        return {
            "required": required,
            "configured": bool(url and token),
        }

    @classmethod
    def _consume_distributed(cls, nonce: str, *, ttl_seconds: int) -> bool | None:
        url, token, required = cls._distributed_settings()
        if not url and not token:
            if required:
                raise LightRAGConfigurationError(
                    "distributed LightRAG nonce storage is required"
                )
            return None
        if not url or not token:
            raise LightRAGConfigurationError(
                "both LightRAG nonce Redis URL and token are required"
            )

        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        prefix = os.getenv(
            "LIGHTRAG_NONCE_KEY_PREFIX", "graphrag:lightrag:nonce"
        ).strip() or "graphrag:lightrag:nonce"
        key = f"{prefix}:{digest}"
        parts = ("SET", key, "1", "NX", "EX", str(max(1, ttl_seconds)))
        endpoint = "/".join(
            [url, *[quote(part.strip("/"), safe="") for part in parts]]
        )
        try:
            timeout = max(
                1.0,
                min(float(os.getenv("LIGHTRAG_NONCE_TIMEOUT_SECONDS", "5")), 10.0),
            )
            response = requests.post(
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                timeout=timeout,
            )
            response.raise_for_status()
            return str(response.json().get("result") or "").upper() == "OK"
        except LightRAGConfigurationError:
            raise
        except Exception as exc:
            # Authentication must fail closed when global replay ownership
            # cannot be established.
            raise LightRAGConfigurationError(
                "distributed LightRAG nonce storage is unavailable"
            ) from exc

    def consume(self, nonce: str, *, expires_at: int, now: int) -> bool:
        with self._lock:
            self._items = {key: expiry for key, expiry in self._items.items() if expiry >= now}
            if nonce in self._items:
                return False

        distributed = self._consume_distributed(
            nonce,
            ttl_seconds=max(1, expires_at - now),
        )
        if distributed is False:
            return False

        with self._lock:
            # Recheck after the network request to fence two threads in a
            # local-only development process.
            if nonce in self._items:
                return False
            self._items[nonce] = expires_at
        return True


def verify_request(
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    secret: str | None = None,
    now: int | None = None,
    max_age_seconds: int | None = None,
    replay_cache: NonceReplayCache | None = None,
) -> None:
    """Fail closed on stale, altered, replayed, or incorrectly signed traffic."""

    lowered = {str(key).lower(): str(value).strip() for key, value in headers.items()}
    timestamp_value = lowered.get(TIMESTAMP_HEADER.lower(), "")
    nonce = lowered.get(NONCE_HEADER.lower(), "")
    supplied_sha = lowered.get(CONTENT_SHA_HEADER.lower(), "")
    supplied_signature = lowered.get(SIGNATURE_HEADER.lower(), "")
    if not timestamp_value or not nonce or not supplied_sha or not supplied_signature:
        raise LightRAGAuthenticationError("missing internal request authentication")
    try:
        timestamp = int(timestamp_value)
    except ValueError as exc:
        raise LightRAGAuthenticationError("invalid internal request timestamp") from exc
    current = int(time.time() if now is None else now)
    max_age = max_age_seconds
    if max_age is None:
        try:
            max_age = int(os.getenv("LIGHTRAG_HMAC_MAX_AGE_SECONDS", "300"))
        except ValueError:
            max_age = 300
    max_age = max(30, min(int(max_age), 900))
    if abs(current - timestamp) > max_age:
        raise LightRAGAuthenticationError("stale internal request")
    if not 12 <= len(nonce) <= 128:
        raise LightRAGAuthenticationError("invalid internal request nonce")
    actual_sha = body_sha256(body)
    if not hmac.compare_digest(actual_sha, supplied_sha):
        raise LightRAGAuthenticationError("internal request body mismatch")
    if not supplied_signature.startswith(f"{SIGNATURE_VERSION}="):
        raise LightRAGAuthenticationError("unsupported internal signature version")
    expected = hmac.new(
        _secret(secret, "LIGHTRAG_HMAC_SECRET"),
        _canonical(method, path, timestamp_value, nonce, actual_sha),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, supplied_signature.split("=", 1)[1]):
        raise LightRAGAuthenticationError("invalid internal request signature")
    if replay_cache and not replay_cache.consume(nonce, expires_at=timestamp + max_age, now=current):
        raise LightRAGAuthenticationError("replayed internal request")
